"""JSON-safe API exposed to the modern Desktop WebView."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

from web_e2e_test_studio.agent.planner import OpenAICompatibleVLMBackend
from web_e2e_test_studio.capabilities import default_capabilities
from web_e2e_test_studio.config import (
    configure_playwright_browsers,
    load_dotenv,
)
from web_e2e_test_studio.desktop.controller import DesktopStudioController
from web_e2e_test_studio.desktop.engine import DesktopRecordingConfig
from web_e2e_test_studio.desktop.recovery import RunCheckpointStore


class StudioBridge:
    """Translate WebView calls into the Studio's threaded command protocol."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        application_root = Path(__file__).resolve().parents[3]
        self.playwright_browsers = configure_playwright_browsers(
            self.workspace,
            application_root=application_root,
        )
        self.dotenv_path = self.workspace / ".env"
        self.dotenv_loaded = load_dotenv(self.dotenv_path)
        self._model_source = (
            str(self.dotenv_path)
            if {"VLM_API_KEY", "OPENAI_API_KEY"} & self.dotenv_loaded
            else "process environment"
        )
        self.controller = DesktopStudioController()
        self._checkpoint_store = RunCheckpointStore(self.workspace)
        self._model_available = False
        self._window: Any | None = None

    def attach_window(self, window: Any) -> None:
        self._window = window

    def bootstrap(self) -> dict[str, Any]:
        return {
            "workspace": str(self.workspace),
            "dotenv": {
                "path": str(self.dotenv_path),
                "loaded": bool(self.dotenv_loaded),
            },
            "playwright": {
                "configured": self.playwright_browsers is not None,
                "path": (
                    str(self.playwright_browsers)
                    if self.playwright_browsers is not None
                    else ""
                ),
                "install_command": (
                    f'"{Path(sys.executable)}" -m playwright install chromium'
                ),
            },
            "model": self.get_model_settings(),
            "recoverable_sessions": self.list_recoverable_sessions(),
            "capabilities": [
                capability.to_dict()
                for capability in default_capabilities()
            ],
        }

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
            )
        )
        return {"accepted": True}

    def discard_session(self, checkpoint_path: str) -> dict[str, bool]:
        self._checkpoint_store.discard(checkpoint_path)
        return {"discarded": True}

    def get_model_settings(self) -> dict[str, Any]:
        api_key = (
            os.environ.get("VLM_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or ""
        )
        return {
            "configured": bool(api_key),
            "key_hint": _key_hint(api_key),
            "model": os.environ.get("VLM_MODEL", "gpt-4o"),
            "base_url": (
                os.environ.get("VLM_BASE_URL")
                or os.environ.get("OPENAI_BASE_URL")
                or ""
            ),
            "source": self._model_source if api_key else "",
            "ignore_proxy": os.environ.get(
                "VLM_IGNORE_PROXY", ""
            ).strip().lower()
            in {"1", "true", "yes", "on"},
        }

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
            self._model_source = "Desktop session settings"
        if bool(config.get("clear_api_key", False)):
            os.environ.pop("VLM_API_KEY", None)
            os.environ.pop("OPENAI_API_KEY", None)
            self._model_source = ""
        os.environ["VLM_MODEL"] = model
        os.environ["VLM_IGNORE_PROXY"] = (
            "true" if ignore_proxy else "false"
        )
        if base_url:
            os.environ["VLM_BASE_URL"] = base_url
        else:
            os.environ.pop("VLM_BASE_URL", None)
            os.environ.pop("OPENAI_BASE_URL", None)
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
            response = OpenAICompatibleVLMBackend.from_env(
                timeout=15.0
            ).complete_json(
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
        output_dir = Path(
            str(config.get("output_dir", self.workspace / "recordings"))
        ).expanduser().resolve()
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
            raise ValueError(
                "Verify the model connection before sending an instruction"
            )
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

    def poll_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for event in self.controller.poll_events():
            item: dict[str, Any] = {
                "kind": event.kind,
                "message": event.message,
                "details": list(event.details),
                "payload": event.payload,
            }
            if event.snapshot is not None:
                snapshot = event.snapshot
                observation = snapshot.last_observation.to_dict()
                observation["url"] = _safe_display_url(
                    snapshot.last_observation.url
                )
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
    if (
        isinstance(expected, bool)
        or not isinstance(expected, int)
        or not 100 <= expected <= 599
    ):
        raise ValueError("Expected HTTP status must be from 100 to 599")
    timeout = request.get("timeout", 30)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or timeout <= 0
    ):
        raise ValueError("HTTP timeout must be greater than zero")


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
