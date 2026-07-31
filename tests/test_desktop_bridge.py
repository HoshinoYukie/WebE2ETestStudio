from __future__ import annotations

import json
import os
from pathlib import Path
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
            "source": "Desktop session settings",
            "ignore_proxy": True,
        }
        assert os.environ["VLM_API_KEY"] == "desktop-secret"
        assert os.environ["VLM_MODEL"] == "vision-model"
        assert os.environ["VLM_BASE_URL"] == "https://vlm.example.test/v1"
        assert os.environ["VLM_IGNORE_PROXY"] == "true"
        assert "api_key" not in configured
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
        assert calls == [
            (r"C:\tools\Simulator.exe", ["--profile", "uat"])
        ]
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
            }
        ]
        with pytest.raises(ValueError, match="absolute http"):
            bridge.run_http_request(
                json.dumps({"method": "GET", "url": "/relative"})
            )
    finally:
        bridge.close()


def test_desktop_bridge_discovers_workspace_playwright_browsers(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
    browser_root = tmp_path / ".playwright-browsers"
    executable = (
        browser_root
        / "chromium-1228"
        / "chrome-win64"
        / "chrome.exe"
    )
    executable.parent.mkdir(parents=True)
    executable.touch()

    bridge = StudioBridge(tmp_path)
    try:
        assert os.environ["PLAYWRIGHT_BROWSERS_PATH"] == str(browser_root)
        assert bridge.bootstrap()["playwright"]["configured"] is True
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
    asset.write_text("{}", encoding="utf-8")
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

        assert sessions[0]["name"] == "recover"
        assert sessions[0]["round"] == 2
        assert sessions[0]["checkpoint_path"] == str(path)
        assert bridge.discard_session(str(path)) == {"discarded": True}
        assert not path.exists()
    finally:
        bridge.close()
