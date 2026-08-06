from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from web_e2e_test_studio.desktop.bridge import (
    StudioBridge,
    screenshot_data_url,
)
from web_e2e_test_studio.desktop.recovery import (
    RunCheckpoint,
    RunCheckpointStore,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    RecordedStep,
    StepSource,
    VerificationResult,
    VerificationStatus,
)
from web_e2e_test_studio.domain import TestAsset as RecordedTestAsset
from web_e2e_test_studio.storage import JsonAssetStore


class _HealthyBackend:
    @classmethod
    def from_env(
        cls,
        *,
        timeout: float | None = None,
    ) -> _HealthyBackend:
        assert timeout == 15.0
        return cls()

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        screenshot_path: str,
    ) -> dict[str, Any]:
        assert "health check" in user_prompt.lower()
        assert screenshot_path == ""
        return {"status": "ok"}


class _UnavailableBackend(_HealthyBackend):
    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        screenshot_path: str,
    ) -> dict[str, Any]:
        raise RuntimeError("deployment was not found")


def test_desktop_bridge_configures_vlm_for_the_current_process(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    for name in ("VLM_API_KEY", "VLM_MODEL", "VLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    bridge = StudioBridge(tmp_path)
    try:
        before = bridge.get_model_settings()
        configured = bridge.configure_model(
            {
                "api_key": "desktop-secret",
                "model": "vision-model",
                "base_url": "https://vlm.example.test/v1",
                "ignore_proxy": True,
            }
        )

        assert before["configured"] is False
        assert configured == {
            "configured": True,
            "key_hint": "desk…cret",
            "model": "vision-model",
            "base_url": "https://vlm.example.test/v1",
            "source": str(bridge.user_dotenv_path),
            "ignore_proxy": True,
        }
        assert os.environ["VLM_API_KEY"] == "desktop-secret"
        assert os.environ["VLM_MODEL"] == "vision-model"
        assert os.environ["VLM_BASE_URL"] == "https://vlm.example.test/v1"
        assert os.environ["VLM_IGNORE_PROXY"] == "true"
        assert "api_key" not in configured
        persisted = bridge.user_dotenv_path.read_text(encoding="utf-8")
        assert 'VLM_API_KEY="desktop-secret"' in persisted
        assert 'VLM_MODEL="vision-model"' in persisted
        assert 'VLM_BASE_URL="https://vlm.example.test/v1"' in persisted
        assert 'VLM_IGNORE_PROXY="true"' in persisted
    finally:
        bridge.close()


def test_desktop_bridge_loads_vlm_settings_from_workspace_dotenv(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    for name in ("VLM_API_KEY", "VLM_MODEL", "VLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# Desktop model settings",
                'VLM_API_KEY="dotenv-secret"',
                "VLM_MODEL=dotenv-model",
                "VLM_BASE_URL=https://dotenv.example.test/v1",
            ]
        ),
        encoding="utf-8",
    )

    bridge = StudioBridge(tmp_path)
    try:
        settings = bridge.get_model_settings()

        assert settings["configured"] is True
        assert settings["key_hint"] == "dote…cret"
        assert settings["model"] == "dotenv-model"
        assert settings["base_url"] == "https://dotenv.example.test/v1"
        assert settings["source"] == str(tmp_path / ".env")
    finally:
        bridge.close()


def test_desktop_bridge_loads_user_model_settings_and_reports_application_data(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    for name in ("VLM_API_KEY", "VLM_MODEL", "VLM_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    data_dir = tmp_path / "user-data"
    monkeypatch.setenv("WEB_E2E_STUDIO_DATA_DIR", str(data_dir))
    config_dir = data_dir / "config"
    config_dir.mkdir(parents=True)
    (config_dir / ".env").write_text(
        "VLM_API_KEY=user-secret\nVLM_MODEL=user-model\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    bridge = StudioBridge(workspace)
    try:
        bootstrap = bridge.bootstrap()
        assert bridge.get_model_settings()["model"] == "user-model"
        assert bridge.get_model_settings()["source"] == str(config_dir / ".env")
        assert bootstrap["application_data"]["path"] == str(data_dir)
        assert Path(bootstrap["application_data"]["browsers"]).is_dir()
    finally:
        bridge.close()


def test_desktop_bridge_checks_model_with_a_real_completion(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.setenv("VLM_API_KEY", "secret")
    monkeypatch.setenv("VLM_MODEL", "gpt-4o")
    monkeypatch.setattr(
        "web_e2e_test_studio.desktop.bridge.OpenAICompatibleVLMBackend",
        _HealthyBackend,
    )
    bridge = StudioBridge(tmp_path)
    try:
        assert bridge.check_model() == {
            "available": True,
            "model": "gpt-4o",
            "message": "Model connection verified",
        }
    finally:
        bridge.close()


def test_desktop_bridge_reports_model_check_failure_without_secrets(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.setenv("VLM_API_KEY", "secret")
    monkeypatch.setenv("VLM_MODEL", "gpt-4o")
    monkeypatch.setattr(
        "web_e2e_test_studio.desktop.bridge.OpenAICompatibleVLMBackend",
        _UnavailableBackend,
    )
    bridge = StudioBridge(tmp_path)
    try:
        result = bridge.check_model()

        assert result["available"] is False
        assert result["model"] == "gpt-4o"
        assert result["message"] == "deployment was not found"
        assert "secret" not in str(result)
    finally:
        bridge.close()


def test_desktop_bridge_rejects_messages_until_model_is_verified(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.setenv("VLM_API_KEY", "secret")
    bridge = StudioBridge(tmp_path)
    try:
        with pytest.raises(ValueError, match="Verify the model connection"):
            bridge.send_message("open the page")

        bridge._model_available = True
        assert bridge.send_message("open the page") == {"accepted": True}
    finally:
        bridge.close()


def test_desktop_bridge_import_and_launch_decodes_json_payload(
    tmp_path: Path,
) -> None:
    bridge = StudioBridge(tmp_path)
    calls: list[tuple[str, list[str]]] = []
    bridge.controller.import_and_launch_tool = (  # type: ignore[method-assign]
        lambda path, arguments: calls.append((path, arguments))
    )
    try:
        assert bridge.import_and_launch_tool(
            json.dumps(
                {
                    "path": r"C:\tools\Simulator.exe",
                    "arguments": ["--profile", "uat"],
                }
            )
        ) == {"accepted": True}
        assert calls == [(r"C:\tools\Simulator.exe", ["--profile", "uat"])]
        with pytest.raises(ValueError, match="Choose an executable"):
            bridge.import_and_launch_tool()
    finally:
        bridge.close()


def test_desktop_bridge_decodes_and_queues_manual_http_request(
    tmp_path: Path,
) -> None:
    bridge = StudioBridge(tmp_path)
    calls: list[dict[str, Any]] = []
    bridge.controller.run_http_request = (  # type: ignore[method-assign]
        lambda request: calls.append(request)
    )
    payload = json.dumps(
        {
            "method": "POST",
            "url": "https://api.example.test/items",
            "headers": {"Authorization": "Bearer test-token"},
            "json": {"name": "book"},
            "expected_status": 201,
            "timeout": 5,
            "response_assertions": [
                {"path": "id", "operator": "equals", "expected": 42}
            ],
        }
    )
    try:
        assert bridge.run_http_request(payload) == {"accepted": True}
        assert calls == [
            {
                "method": "POST",
                "url": "https://api.example.test/items",
                "headers": {"Authorization": "Bearer test-token"},
                "json": {"name": "book"},
                "expected_status": 201,
                "timeout": 5,
                "response_assertions": [
                    {"path": "id", "operator": "equals", "expected": 42}
                ],
            }
        ]
        with pytest.raises(ValueError, match="absolute http"):
            bridge.run_http_request(json.dumps({"method": "GET", "url": "/relative"}))
        with pytest.raises(ValueError, match="unsupported operator"):
            bridge.run_http_request(
                json.dumps(
                    {
                        "method": "GET",
                        "url": "https://api.example.test",
                        "response_assertions": [{"path": "id", "operator": "matches"}],
                    }
                )
            )
    finally:
        bridge.close()


def test_desktop_bridge_chooses_session_output_directory(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    selected = tmp_path / "recordings"

    class Window:
        def create_file_dialog(self, dialog_type, **options):  # noqa: ANN001, ANN003
            assert dialog_type == "folder-dialog"
            assert options == {"allow_multiple": False}
            return (str(selected),)

    monkeypatch.setitem(
        sys.modules,
        "webview",
        SimpleNamespace(FileDialog=SimpleNamespace(FOLDER="folder-dialog")),
    )
    bridge = StudioBridge(tmp_path)
    bridge.attach_window(Window())
    try:
        assert bridge.choose_output_directory() == {"path": str(selected)}
    finally:
        bridge.close()


def test_desktop_bridge_discovers_workspace_playwright_browsers(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    browser_root = tmp_path / ".playwright-browsers"
    executable = browser_root / "chromium-1228" / "chrome-win64" / "chrome.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()

    bridge = StudioBridge(tmp_path)
    try:
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browser_root)
        assert bridge.bootstrap()["playwright"]["configured"] is True
    finally:
        bridge.close()


def test_desktop_bridge_discovers_user_managed_playwright_browsers(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    data_dir = tmp_path / "application-data"
    monkeypatch.setenv("WEB_E2E_STUDIO_DATA_DIR", str(data_dir))
    browser_root = data_dir / "browsers"
    executable = browser_root / "chromium-1228" / "chrome-win64" / "chrome.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()

    bridge = StudioBridge(tmp_path)
    try:
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browser_root)
        assert bridge.bootstrap()["playwright"]["path"] == str(browser_root)
    finally:
        bridge.close()


def test_desktop_bridge_encodes_preview_without_file_url(
    tmp_path: Path,
) -> None:
    screenshot = tmp_path / "observation.png"
    screenshot.write_bytes(b"\x89PNG\r\n\x1a\npreview")

    encoded = screenshot_data_url(screenshot)

    assert encoded.startswith("data:image/png;base64,")
    assert "file:///" not in encoded


def test_desktop_bridge_lists_and_discards_recoverable_sessions(
    tmp_path: Path,
) -> None:
    asset = tmp_path / "recordings" / "recover.json"
    asset.parent.mkdir(parents=True)
    JsonAssetStore().save(
        RecordedTestAsset(
            id="asset_recover",
            name="recover",
            goal="Continue recording",
            steps=[
                RecordedStep(
                    id="step_navigate",
                    action=Action(
                        "navigate",
                        ActionChannel.BROWSER,
                        arguments={"url": "https://example.test/proposals"},
                    ),
                    intent="Open proposals",
                    expected_result="Proposals page opens",
                    source=StepSource.USER,
                    verification=VerificationResult(
                        VerificationStatus.VERIFIED,
                        "Navigation succeeded",
                    ),
                )
            ],
        ),
        asset,
    )
    checkpoint = RunCheckpoint.create(
        name="recover",
        goal="Continue recording",
        asset_path=asset,
        output_path=asset.parent / "test_recover.py",
        workspace=tmp_path,
        initial_url="https://example.test",
        headless=False,
    ).updated(state="suspended", round_number=2)
    path = RunCheckpointStore(tmp_path).save(checkpoint)
    bridge = StudioBridge(tmp_path)
    try:
        sessions = bridge.list_recoverable_sessions()
        details = bridge.inspect_session(str(path))

        assert sessions[0]["name"] == "recover"
        assert sessions[0]["round"] == 2
        assert sessions[0]["checkpoint_path"] == str(path)
        assert details["name"] == "recover"
        assert details["goal"] == "Continue recording"
        assert details["state"] == "suspended"
        assert details["round"] == 2
        assert details["steps"] == [
            {
                "id": "step_navigate",
                "index": 1,
                "action": "navigate",
                "channel": "browser",
                "arguments": {"url": "https://example.test/proposals"},
                "locator": None,
                "intent": "Open proposals",
                "expected_result": "Proposals page opens",
                "source": "user",
                "verification": {
                    "status": "verified",
                    "reason": "Navigation succeeded",
                    "evidence": {},
                },
                "recorded_at": details["steps"][0]["recorded_at"],
            }
        ]
        assert bridge.discard_session(str(path)) == {"discarded": True}
        assert not path.exists()
    finally:
        bridge.close()


def test_desktop_bridge_loads_historical_action_and_requires_side_effect_confirmation(
    tmp_path: Path,
) -> None:
    asset_path = tmp_path / "recordings" / "api.json"
    asset_path.parent.mkdir(parents=True)
    JsonAssetStore().save(
        RecordedTestAsset(
            id="asset_api",
            name="api",
            goal="Run API step",
            steps=[
                RecordedStep(
                    id="step_api",
                    action=Action(
                        "http_request",
                        ActionChannel.API,
                        arguments={"method": "GET", "url": "https://api.example.test"},
                    ),
                    intent="Fetch resource",
                    expected_result="HTTP 200",
                    source=StepSource.USER,
                    verification=VerificationResult(
                        VerificationStatus.VERIFIED,
                        "Request succeeded",
                    ),
                )
            ],
        ),
        asset_path,
    )
    checkpoint = RunCheckpoint.create(
        name="api",
        goal="Run API step",
        asset_path=asset_path,
        output_path=asset_path.parent / "test_api.py",
        workspace=tmp_path,
        initial_url="",
        headless=False,
    )
    checkpoint_path = RunCheckpointStore(tmp_path).save(checkpoint)
    bridge = StudioBridge(tmp_path)
    captured: list[tuple[object, str]] = []
    try:
        with pytest.raises(ValueError, match="require confirmation"):
            bridge.run_historical_step(
                {"checkpoint_path": str(checkpoint_path), "step_id": "step_api"}
            )
        bridge.controller.resume_and_run_step = (  # type: ignore[method-assign]
            lambda config, step_id: captured.append((config, step_id))
        )

        accepted = bridge.run_historical_step(
            {
                "checkpoint_path": str(checkpoint_path),
                "step_id": "step_api",
                "confirmed": True,
            }
        )

        assert accepted == {"accepted": True}
        assert captured[0][1] == "step_api"
        assert captured[0][0].asset_path == asset_path.resolve()  # type: ignore[union-attr]
    finally:
        bridge.close()
