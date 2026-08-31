"""JSON-safe API exposed to the modern Desktop WebView."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import sys
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

from web_e2e_test_studio.agent.planner import OpenAICompatibleVLMBackend
from web_e2e_test_studio.api.response_assertions import parse_response_assertions
from web_e2e_test_studio.capabilities import default_capabilities
from web_e2e_test_studio.config import (
    configure_playwright_browsers,
    load_dotenv,
    read_dotenv,
    update_dotenv,
)
from web_e2e_test_studio.desktop.controller import DesktopStudioController
from web_e2e_test_studio.desktop.engine import DesktopRecordingConfig
from web_e2e_test_studio.desktop.recovery import RunCheckpointStore
from web_e2e_test_studio.runtime_environment import RuntimeEnvironmentManager
from web_e2e_test_studio.runtime_paths import RuntimePaths
from web_e2e_test_studio.storage import JsonAssetStore
from web_e2e_test_studio.workflow_runner import run_markdown_workflow
from web_e2e_test_studio.workflow_spec import WorkflowDocumentStore


class StudioBridge:
    """Translate WebView calls into the Studio's threaded command protocol."""

    def __init__(
        self,
        workspace: Path,
        *,
        runtime_paths: RuntimePaths | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.runtime_paths = runtime_paths or RuntimePaths.discover()
        self.runtime_paths.ensure()
        self.playwright_browsers = configure_playwright_browsers(
            self.workspace,
            application_root=self.runtime_paths.application_dir,
            managed_roots=(self.runtime_paths.browser_dir,),
            bundled_roots=self.runtime_paths.bundled_browser_candidates,
        )
        self.runtime_environment = RuntimeEnvironmentManager(
            self.workspace,
            self.runtime_paths,
        )
        self.user_dotenv_path = self.runtime_paths.user_env_path
        self.user_dotenv_loaded = load_dotenv(self.user_dotenv_path)
        self.dotenv_path = self.workspace / ".env"
        self.dotenv_loaded = load_dotenv(self.dotenv_path)
        self._model_source = (
            str(self.dotenv_path)
            if {"VLM_API_KEY", "OPENAI_API_KEY"} & self.dotenv_loaded
            else (
                str(self.user_dotenv_path)
                if {"VLM_API_KEY", "OPENAI_API_KEY"} & self.user_dotenv_loaded
                else "process environment"
            )
        )
        self.controller = DesktopStudioController()
        self._checkpoint_store = RunCheckpointStore(self.workspace)
        self._model_available = False
        self._window: Any | None = None
        self._workflow_store = WorkflowDocumentStore(self.workspace)
        self._workflow_events: list[dict[str, Any]] = []
        self._workflow_events_lock = threading.Lock()

    def attach_window(self, window: Any) -> None:
        self._window = window

    def bootstrap(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "application_data": {
                "path": str(self.runtime_paths.data_dir),
                "config": str(self.runtime_paths.config_dir),
                "browsers": str(self.runtime_paths.browser_dir),
                "sessions": str(self.runtime_paths.session_dir),
                "logs": str(self.runtime_paths.log_dir),
                "cache": str(self.runtime_paths.cache_dir),
            },
            "dotenv": {
                "path": str(self.dotenv_path),
                "loaded": bool(self.dotenv_loaded),
                "user_path": str(self.user_dotenv_path),
                "user_loaded": bool(self.user_dotenv_loaded),
            },
            "playwright": {
                "configured": self.playwright_browsers is not None,
                "path": (
                    str(self.playwright_browsers) if self.playwright_browsers is not None else ""
                ),
                "install_command": (f'"{Path(sys.executable)}" -m playwright install chromium'),
            },
            "environment": self.check_environment(),
            "environment_settings": self.get_environment_settings(),
            "model": self.get_model_settings(),
            "recoverable_sessions": self.list_recoverable_sessions(),
            "capabilities": [capability.to_dict() for capability in default_capabilities()],
            "workflows": self.list_workflows(),
        }

    def check_environment(self) -> dict[str, Any]:
        """Inspect product runtime dependencies without business assumptions."""
        return self.runtime_environment.inspect()

    def install_environment_component(self, component: str) -> dict[str, Any]:
        """Repair one allow-listed product runtime component."""
        if component != "chromium":
            raise ValueError(f"Environment component is not repairable: {component}")
        result = self.runtime_environment.install_chromium()
        self.playwright_browsers = self.runtime_environment.browser_root
        return result

    def list_workflows(self) -> list[dict[str, Any]]:
        workflows: list[dict[str, Any]] = []
        for path in sorted(self.workspace.rglob("*.workflow.md")):
            try:
                document = self._workflow_store.load(path)
            except (OSError, ValueError):
                continue
            workflows.append(
                {
                    "path": str(path.relative_to(self.workspace)),
                    "name": document["name"],
                    "goal": document["goal"],
                }
            )
        return workflows

    def load_workflow(self, path: str) -> dict[str, Any]:
        """Return one Markdown workflow as form-friendly structured data."""
        return self._workflow_store.load(path)

    def update_workflow_data(self, payload: Any = None) -> dict[str, Any]:
        """Update only the YAML data map while preserving Markdown knowledge."""
        request = _decode_object_payload(payload, "Workflow update")
        path = str(request.get("path", "")).strip()
        updates = request.get("data")
        if not path or not isinstance(updates, dict):
            raise ValueError("Workflow path and data object are required")
        return self._workflow_store.update_data(path, updates)

    def compile_workflow(self, path: str) -> dict[str, Any]:
        """Validate and preview the exact instruction produced by a workflow."""
        document_path = self._workflow_store.load(path)["path"]
        from web_e2e_test_studio.workflow_spec import MarkdownWorkflowSpec

        compiled = MarkdownWorkflowSpec.load(str(document_path)).compile()
        return {
            "name": compiled.name,
            "start_url": compiled.start_url,
            "goal": compiled.goal,
            "steps": list(compiled.steps),
            "instruction": compiled.sanitized_instruction(),
        }

    def run_workflow(self, payload: Any = None) -> dict[str, bool]:
        """Run a saved workflow asynchronously and publish result events."""
        request = _decode_object_payload(payload, "Workflow run")
        path = str(request.get("path", "")).strip()
        if not path:
            raise ValueError("Workflow path is required")
        source = Path(str(self._workflow_store.load(path)["path"]))
        output_dir = Path(
            str(request.get("output_dir", self.workspace / "recordings"))
        ).resolve()

        def execute() -> None:
            try:
                result = run_markdown_workflow(
                    source,
                    output_dir=output_dir,
                    headless=bool(request.get("headless", False)),
                )
                event = {
                    "kind": "workflow_finished",
                    "message": f"Workflow finished: {result.status.value}",
                    "details": [],
                    "payload": {
                        "status": result.status.value,
                        "asset_path": str(result.asset_path),
                        "output_path": str(result.output_path),
                        "summary": result.summary,
                        "question": result.question,
                    },
                }
            except Exception as exc:
                event = {
                    "kind": "error",
                    "message": f"Workflow failed: {exc}",
                    "details": [],
                    "payload": {"command": "run_workflow"},
                }
            with self._workflow_events_lock:
                self._workflow_events.append(event)

        threading.Thread(
            target=execute,
            name="web-e2e-markdown-workflow",
            daemon=True,
        ).start()
        return {"accepted": True}

    def list_recoverable_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "id": checkpoint.id,
                "name": checkpoint.name,
                "goal": checkpoint.goal,
                "state": checkpoint.state,
                "round": checkpoint.round_number,
                "last_url": checkpoint.last_url,
                "pending_question": checkpoint.pending_question,
                "updated_at": checkpoint.updated_at,
                "checkpoint_path": str(checkpoint.checkpoint_path),
            }
            for checkpoint in self._checkpoint_store.discover()
        ]

    def resume_session(self, checkpoint_path: str) -> dict[str, bool]:
        checkpoint = self._checkpoint_store.load(checkpoint_path)
        self.controller.resume(
            DesktopRecordingConfig(
                url=checkpoint.initial_url,
                goal=checkpoint.goal,
                name=checkpoint.name,
                asset_path=checkpoint.asset_path,
                output_path=checkpoint.output_path,
                workspace=checkpoint.workspace,
                headless=checkpoint.headless,
                checkpoint_path=Path(checkpoint_path).resolve(),
            )
        )
        return {"accepted": True}

    def run_historical_step(self, payload: Any = None) -> dict[str, bool]:
        """Resume a persisted session and execute one server-loaded step."""
        request = _decode_object_payload(payload, "Historical step")
        checkpoint_path = str(request.get("checkpoint_path", "")).strip()
        step_id = str(request.get("step_id", "")).strip()
        if not checkpoint_path or not step_id:
            raise ValueError("Checkpoint path and step ID are required")
        checkpoint = self._checkpoint_store.load(checkpoint_path)
        asset = JsonAssetStore().load(checkpoint.asset_path)
        step = next((item for item in asset.steps if item.id == step_id), None)
        if step is None:
            raise ValueError(f"Recorded step not found: {step_id}")
        if step.action.channel.value in {"api", "external"} and not bool(
            request.get("confirmed", False)
        ):
            raise ValueError("API and external tool steps require confirmation")
        self.controller.resume_and_run_step(
            DesktopRecordingConfig(
                url=checkpoint.initial_url,
                goal=checkpoint.goal,
                name=checkpoint.name,
                asset_path=checkpoint.asset_path,
                output_path=checkpoint.output_path,
                workspace=checkpoint.workspace,
                headless=checkpoint.headless,
                checkpoint_path=Path(checkpoint_path).resolve(),
            ),
            step_id,
        )
        return {"accepted": True}

    def inspect_session(self, checkpoint_path: str) -> dict[str, Any]:
        """Load persisted metadata without starting an engine or browser."""
        checkpoint = self._checkpoint_store.load(checkpoint_path)
        asset = JsonAssetStore().load(checkpoint.asset_path)
        return {
            "id": checkpoint.id,
            "name": checkpoint.name,
            "goal": checkpoint.goal,
            "state": checkpoint.state,
            "round": checkpoint.round_number,
            "last_url": _safe_display_url(checkpoint.last_url),
            "pending_question": checkpoint.pending_question,
            "last_error": checkpoint.last_error,
            "updated_at": checkpoint.updated_at,
            "asset_path": str(checkpoint.asset_path),
            "output_path": str(checkpoint.output_path),
            "checkpoint_path": str(checkpoint.checkpoint_path),
            "steps": [
                {
                    "id": step.id,
                    "index": index,
                    "action": step.action.name,
                    "channel": step.action.channel.value,
                    "arguments": _sanitize_step_value(dict(step.action.arguments)),
                    "locator": (
                        step.action.locator.to_dict()
                        if step.action.locator is not None
                        else None
                    ),
                    "intent": step.intent,
                    "expected_result": step.expected_result,
                    "source": step.source.value,
                    "verification": step.verification.to_dict(),
                    "recorded_at": step.recorded_at,
                }
                for index, step in enumerate(asset.steps, start=1)
            ],
        }

    def discard_session(self, checkpoint_path: str) -> dict[str, bool]:
        self._checkpoint_store.discard(checkpoint_path)
        return {"discarded": True}

    def get_model_settings(self) -> dict[str, Any]:
        api_key = os.environ.get("VLM_API_KEY") or os.environ.get("OPENAI_API_KEY") or ""
        return {
            "configured": bool(api_key),
            "key_hint": _key_hint(api_key),
            "model": os.environ.get("VLM_MODEL", "gpt-4o"),
            "base_url": (os.environ.get("VLM_BASE_URL") or os.environ.get("OPENAI_BASE_URL") or ""),
            "source": self._model_source if api_key else "",
            "ignore_proxy": os.environ.get("VLM_IGNORE_PROXY", "").strip().lower()
            in {"1", "true", "yes", "on"},
        }

    def get_environment_settings(self) -> dict[str, Any]:
        """Return persisted variables with secret-shaped values masked."""
        values: dict[str, tuple[str, str]] = {
            name: (value, "user")
            for name, value in read_dotenv(self.user_dotenv_path).items()
        }
        for name, value in read_dotenv(self.dotenv_path).items():
            values.setdefault(name, (value, "workspace"))
        variables = []
        for name, (value, source) in sorted(values.items()):
            masked = _is_sensitive_variable(name)
            variables.append(
                {
                    "name": name,
                    "value": "" if masked else value,
                    "masked": masked,
                    "configured": True,
                    "source": source,
                }
            )
        return {"path": str(self.user_dotenv_path), "variables": variables}

    def configure_environment_variable(self, config: dict[str, Any]) -> dict[str, Any]:
        """Create, replace, or remove one generic persisted environment value."""
        name = str(config.get("name", "")).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(
                "Environment variable name must use letters, digits, and underscores"
            )
        remove = bool(config.get("remove", False))
        value = str(config.get("value", ""))
        if remove:
            os.environ.pop(name, None)
            update_dotenv(self.user_dotenv_path, {name: None})
        else:
            os.environ[name] = value
            update_dotenv(self.user_dotenv_path, {name: value})
        self.user_dotenv_loaded = set(read_dotenv(self.user_dotenv_path))
        self._model_available = False
        return self.get_environment_settings()

    def configure_model(self, config: dict[str, Any]) -> dict[str, Any]:
        api_key = str(config.get("api_key", "")).strip()
        model = str(config.get("model", "")).strip()
        base_url = str(config.get("base_url", "")).strip()
        ignore_proxy = bool(config.get("ignore_proxy", False))
        if not model:
            raise ValueError("Model name is required")
        if base_url:
            parsed = urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("Base URL must be an http(s) URL")
        if api_key:
            os.environ["VLM_API_KEY"] = api_key
            self._model_source = str(self.user_dotenv_path)
        if bool(config.get("clear_api_key", False)):
            os.environ.pop("VLM_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            self._model_source = ""
        os.environ["VLM_MODEL"] = model
        os.environ["VLM_IGNORE_PROXY"] = "true" if ignore_proxy else "false"
        if base_url:
            os.environ["VLM_BASE_URL"] = base_url
        else:
            os.environ.pop("VLM_BASE_URL", None)
            os.environ.pop("OPENAI_BASE_URL", None)
        persisted: dict[str, str | None] = {
            "VLM_MODEL": model,
            "VLM_BASE_URL": base_url or None,
            "VLM_IGNORE_PROXY": "true" if ignore_proxy else "false",
        }
        if api_key:
            persisted["VLM_API_KEY"] = api_key
        elif bool(config.get("clear_api_key", False)):
            persisted["VLM_API_KEY"] = None
        update_dotenv(self.user_dotenv_path, persisted)
        self._model_available = False
        return self.get_model_settings()

    def check_model(self) -> dict[str, Any]:
        """Perform a real, minimal completion before enabling agent commands."""
        settings = self.get_model_settings()
        self._model_available = False
        if not settings["configured"]:
            return {
                "available": False,
                "model": settings["model"],
                "message": "Configure an API key before checking the model",
            }
        try:
            response = OpenAICompatibleVLMBackend.from_env(timeout=15.0).complete_json(
                "Return only a JSON object with status set to ok.",
                "Desktop Studio model health check.",
                "",
            )
            if response.get("status") != "ok":
                raise ValueError("Model returned an invalid health-check response")
        except Exception as exc:  # provider errors must be shown in the UI
            return {
                "available": False,
                "model": settings["model"],
                "message": _safe_model_error(exc),
            }
        self._model_available = True
        return {
            "available": True,
            "model": settings["model"],
            "message": "Model connection verified",
        }

    def start_session(self, config: dict[str, Any]) -> dict[str, bool]:
        name = str(config.get("name", "")).strip() or "recorded_flow"
        output_dir = (
            Path(str(config.get("output_dir", self.workspace / "recordings")))
            .expanduser()
            .resolve()
        )
        self.controller.start(
            DesktopRecordingConfig(
                url=str(config.get("url", "")).strip(),
                goal=str(config.get("goal", "")).strip(),
                name=name,
                asset_path=output_dir / f"{name}.json",
                output_path=output_dir / f"test_{name}.py",
                workspace=self.workspace,
                headless=bool(config.get("headless", False)),
            )
        )
        return {"accepted": True}

    def send_message(self, message: str) -> dict[str, bool]:
        if not self._model_available:
            raise ValueError("Verify the model connection before sending an instruction")
        self.controller.run_ai(message)
        return {"accepted": True}

    def sync_manual(self) -> dict[str, bool]:
        self.controller.sync_manual()
        return {"accepted": True}

    def activate_page(self, page_id: str) -> dict[str, bool]:
        self.controller.activate_page(page_id)
        return {"accepted": True}

    def save(self) -> dict[str, bool]:
        self.controller.save()
        return {"accepted": True}

    def validate_export(self) -> dict[str, bool]:
        self.controller.validate_export()
        return {"accepted": True}

    def define_variable(self, payload: Any = None) -> dict[str, bool]:
        config = _decode_object_payload(payload, "Generated variable")
        name = str(config.get("name", "")).strip()
        description = str(config.get("description", "")).strip()
        if not name or not description:
            raise ValueError("Variable name and format description are required")
        self.controller.define_variable(name, description)
        return {"accepted": True}

    def extract_variable(self, payload: Any = None) -> dict[str, bool]:
        config = _decode_object_payload(payload, "Extracted variable")
        name = str(config.get("name", "")).strip()
        description = str(config.get("description", "")).strip()
        source = str(config.get("source", "")).strip()
        file_path = str(config.get("file_path", "")).strip()
        if not name or not description:
            raise ValueError("Variable name and extraction description are required")
        if source not in {"page", "spreadsheet"}:
            raise ValueError("Variable source must be page or spreadsheet")
        if source == "spreadsheet" and not file_path:
            raise ValueError("Spreadsheet extraction requires a file path")
        self.controller.extract_variable(name, description, source, file_path)
        return {"accepted": True}

    def stop(self) -> dict[str, bool]:
        self.controller.stop()
        return {"accepted": True}

    def cancel(self) -> dict[str, bool]:
        return {"accepted": self.controller.cancel()}

    def register_tool(self, path: str) -> dict[str, bool]:
        self.controller.register_tool(path)
        return {"accepted": True}

    def run_tool(self, tool_ref: str, arguments: list[str]) -> dict[str, bool]:
        self.controller.run_tool(tool_ref, arguments)
        return {"accepted": True}

    def run_http_request(self, payload: Any = None) -> dict[str, bool]:
        request = _decode_object_payload(payload, "HTTP request")
        _validate_http_request(request)
        self.controller.run_http_request(request)
        return {"accepted": True}

    def import_and_launch_tool(
        self,
        payload: Any = None,
    ) -> dict[str, bool]:
        config = _decode_launch_payload(payload)
        path = str(config.get("path", "")).strip()
        raw_arguments = config.get("arguments", [])
        if not isinstance(raw_arguments, list) or not all(
            isinstance(item, str) for item in raw_arguments
        ):
            raise ValueError("Launch arguments must be a list of strings")
        if not path.strip():
            raise ValueError("Choose an executable before launching it")
        self.controller.import_and_launch_tool(path, raw_arguments)
        return {"accepted": True}

    def choose_application(self) -> dict[str, str]:
        if self._window is None:
            return {"path": ""}
        import webview

        selected = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=("Windows executable (*.exe)",),
        )
        return {"path": str(selected[0]) if selected else ""}

    def choose_spreadsheet(self) -> dict[str, str]:
        if self._window is None:
            return {"path": ""}
        import webview

        selected = self._window.create_file_dialog(
            webview.FileDialog.OPEN,
            allow_multiple=False,
            file_types=("Excel workbook (*.xlsx;*.xlsm)",),
        )
        return {"path": str(selected[0]) if selected else ""}

    def choose_output_directory(self) -> dict[str, str]:
        """Open the native directory picker for a recording session."""
        if self._window is None:
            return {"path": ""}
        import webview

        selected = self._window.create_file_dialog(
            webview.FileDialog.FOLDER,
            allow_multiple=False,
        )
        return {"path": str(selected[0]) if selected else ""}

    def poll_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        with self._workflow_events_lock:
            events.extend(self._workflow_events)
            self._workflow_events.clear()
        for event in self.controller.poll_events():
            payload = dict(event.payload)
            screenshot_path = payload.pop("screenshot_path", "")
            if isinstance(screenshot_path, str) and screenshot_path:
                payload["screenshot_data_url"] = screenshot_data_url(screenshot_path)
            if "url" in payload:
                payload["url"] = _safe_display_url(str(payload["url"]))
            item: dict[str, Any] = {
                "kind": event.kind,
                "message": event.message,
                "details": list(event.details),
                "payload": payload,
            }
            if event.snapshot is not None:
                snapshot = event.snapshot
                observation = snapshot.last_observation.to_dict()
                observation["url"] = _safe_display_url(snapshot.last_observation.url)
                observation["screenshot_data_url"] = screenshot_data_url(
                    snapshot.last_observation.screenshot_path
                )
                item["snapshot"] = {
                    "running": snapshot.running,
                    "step_count": snapshot.step_count,
                    "asset_path": str(snapshot.asset_path),
                    "output_path": str(snapshot.output_path),
                    "observation": observation,
                    "active_page_id": snapshot.active_page_id,
                    "variables": list(snapshot.variables),
                    "pages": [
                        {
                            "id": page.id,
                            "url": _safe_display_url(page.url),
                            "title": page.title,
                            "active": page.active,
                            "opener_id": page.opener_id,
                        }
                        for page in snapshot.pages
                    ],
                    "steps": [
                        {
                            "id": step.id,
                            "source": step.source.value,
                            "channel": step.action.channel.value,
                            "action": step.action.name,
                            "intent": step.intent,
                            "arguments": _sanitize_step_value(dict(step.action.arguments)),
                            "locator": (
                                step.action.locator.to_dict()
                                if step.action.locator is not None
                                else None
                            ),
                            "expected_result": step.expected_result,
                            "verification": step.verification.to_dict(),
                        }
                        for step in snapshot.steps
                    ],
                }
            events.append(item)
        return events

    def close(self) -> None:
        self.controller.close()


__all__ = ["StudioBridge"]


def _decode_launch_payload(payload: Any) -> dict[str, Any]:
    return _decode_object_payload(payload, "Application launch")


def _safe_display_url(value: str) -> str:
    """Hide query and fragment secrets from the Desktop presentation layer."""
    if not value:
        return ""
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _sanitize_step_value(value: Any, *, key: str = "") -> Any:  # noqa: ANN401
    if key.casefold() in {
        "api_key",
        "authorization",
        "cookie",
        "password",
        "secret",
        "token",
        "x-api-key",
    }:
        return "[REDACTED]"
    if isinstance(value, list):
        return [_sanitize_step_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_step_value(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    return value


def _decode_object_payload(
    payload: Any,
    label: str,
) -> dict[str, Any]:
    if payload is None or payload == "":
        return {}
    if isinstance(payload, dict):
        return payload
    if not isinstance(payload, str):
        raise ValueError(f"{label} payload must be JSON text")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} payload is invalid JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError(f"{label} payload must be a JSON object")
    return decoded


def _validate_http_request(request: dict[str, Any]) -> None:
    method = request.get("method")
    if not isinstance(method, str) or method.upper() not in {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS",
    }:
        raise ValueError("HTTP method is not supported")
    url = request.get("url")
    if not isinstance(url, str):
        raise ValueError("HTTP URL is required")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("HTTP URL must be an absolute http(s) URL")
    headers = request.get("headers", {})
    if not isinstance(headers, dict):
        raise ValueError("HTTP headers must be a JSON object")
    if "json" in request and "body" in request:
        raise ValueError("Choose either JSON or raw body, not both")
    expected = request.get("expected_status", 200)
    if isinstance(expected, bool) or not isinstance(expected, int) or not 100 <= expected <= 599:
        raise ValueError("Expected HTTP status must be from 100 to 599")
    timeout = request.get("timeout", 30)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ValueError("HTTP timeout must be greater than zero")
    parse_response_assertions(request.get("response_assertions"))


def _safe_model_error(error: Exception) -> str:
    message = str(error).strip() or error.__class__.__name__
    for secret_name in ("VLM_API_KEY", "OPENAI_API_KEY"):
        secret = os.environ.get(secret_name, "")
        if secret:
            message = message.replace(secret, "[redacted]")
    return message[:500]


def _key_hint(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "••••••••"
    return f"{value[:4]}…{value[-4:]}"


def _is_sensitive_variable(name: str) -> bool:
    normalized = name.upper()
    return bool(
        re.search(
            r"(?:^|_)(?:PASSWORD|PASS|SECRET|TOKEN|API_?KEY|PRIVATE_KEY|ACCESS_KEY|CREDENTIALS?)(?:_|$)",
            normalized,
        )
    )


def screenshot_data_url(
    path: str | Path,
    *,
    max_bytes: int = 5 * 1024 * 1024,
) -> str:
    """Encode one observation image for WebView without local-file access."""
    if not path:
        return ""
    source = Path(path)
    if not source.is_file() or source.stat().st_size > max_bytes:
        return ""
    mime = mimetypes.guess_type(source.name)[0] or "image/png"
    if not mime.startswith("image/"):
        return ""
    encoded = base64.b64encode(source.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{encoded}"
