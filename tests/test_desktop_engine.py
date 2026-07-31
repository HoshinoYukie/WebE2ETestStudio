from __future__ import annotations

import json
import runpy
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from web_e2e_test_studio.agent import ActionSpec
from web_e2e_test_studio.desktop.engine import (
    DesktopRecordingConfig,
    DesktopRecordingEngine,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    Locator,
    Observation,
)


class FakePage:
    pass


class FakeDevice:
    def __init__(self) -> None:
        self.page = FakePage()
        self.closed = False
        self.started = False
        self.executed: list[Action] = []
        self.url = "about:blank"

    def start(self) -> FakeDevice:
        self.started = True
        return self

    def close(self) -> None:
        self.closed = True

    def observe(self) -> Observation:
        return Observation(
            url=self.url,
            title="Dashboard",
            screenshot_path="observation.png",
        )

    def execute(self, action: Action) -> ActionResult:
        self.executed.append(action)
        if action.name == "navigate":
            self.url = str(action.arguments["url"])
        return ActionResult.succeeded({"action": action.name})

    @staticmethod
    def action_specs() -> tuple[ActionSpec, ...]:
        return (
            ActionSpec(
                name="navigate",
                channel=ActionChannel.BROWSER,
                description="Navigate",
                required_arguments=("url",),
            ),
            ActionSpec(
                name="click",
                channel=ActionChannel.BROWSER,
                description="Click",
                locator_required=True,
            ),
        )


class FakeRecorder:
    def __init__(self) -> None:
        self.installed_on = None
        self.pending: list[Action] = []

    def install(self, page) -> None:  # noqa: ANN001
        self.installed_on = page

    def drain(self, page) -> list[Action]:  # noqa: ANN001
        assert page is self.installed_on
        drained, self.pending = self.pending, []
        return drained


def test_desktop_engine_records_manual_actions_and_exports_pytest(
    tmp_path: Path,
) -> None:
    device = FakeDevice()
    recorder = FakeRecorder()
    config = DesktopRecordingConfig(
        url="https://example.test",
        goal="Open the dashboard",
        name="desktop_flow",
        asset_path=tmp_path / "desktop_flow.json",
        output_path=tmp_path / "test_desktop_flow.py",
        workspace=tmp_path,
    )
    engine = DesktopRecordingEngine(
        config,
        device=device,
        recorder=recorder,
    )

    started = engine.start()
    recorder.pending.append(
        Action(
            name="click",
            channel=ActionChannel.BROWSER,
            locator=Locator("role", "button", name="Continue"),
        )
    )
    synced = engine.sync_manual()
    saved = engine.save()
    engine.stop()

    assert started.running is True
    assert started.step_count == 1
    assert synced.step_count == 2
    assert synced.last_observation.title == "Dashboard"
    assert saved.asset_path == config.asset_path.resolve()
    assert saved.output_path == config.output_path.resolve()
    assert config.asset_path.exists()
    exported = config.output_path.read_text(encoding="utf-8")
    assert "def test_desktop_flow" in exported
    assert "openai" not in exported.lower()
    assert device.closed is True


def test_desktop_engine_can_start_without_a_browser_for_api_or_tool_tasks(
    tmp_path: Path,
) -> None:
    device = FakeDevice()
    engine = DesktopRecordingEngine(
        DesktopRecordingConfig(
            url="",
            goal="Call an API and run a tool",
            name="api_only",
            asset_path=tmp_path / "api_only.json",
            output_path=tmp_path / "test_api_only.py",
            workspace=tmp_path,
        ),
        device=device,
        recorder=FakeRecorder(),
    )

    snapshot = engine.start()
    engine.stop()

    assert snapshot.running is True
    assert snapshot.step_count == 0
    assert device.executed == []
    assert device.closed is False
    exported = (tmp_path / "test_api_only.py").read_text(encoding="utf-8")
    assert "playwright" not in exported


def test_browser_capability_starts_lazily_after_api_only_session(
    tmp_path: Path,
) -> None:
    device = FakeDevice()
    recorder = FakeRecorder()
    engine = DesktopRecordingEngine(
        DesktopRecordingConfig(
            url="",
            goal="Navigate when requested later",
            name="lazy_browser",
            asset_path=tmp_path / "lazy_browser.json",
            output_path=tmp_path / "test_lazy_browser.py",
            workspace=tmp_path,
        ),
        device=device,
        recorder=recorder,
    )
    engine.start()

    result = engine.execute(
        Action(
            name="navigate",
            channel=ActionChannel.BROWSER,
            arguments={"url": "https://example.test/later"},
        )
    )
    snapshot = engine.save()
    engine.stop()

    assert result.success
    assert device.started is True
    assert recorder.installed_on is device.page
    assert snapshot.last_observation.url == "https://example.test/later"


def test_desktop_engine_imports_launches_and_records_executable_by_path(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    executable = tmp_path / "tools" / "Simulator.exe"
    executable.parent.mkdir()
    executable.write_bytes(b"MZ")

    class _StartedProcess:
        pid = 9123

        def poll(self) -> None:
            return None

    monkeypatch.setattr(
        "subprocess.Popen",
        lambda *args, **kwargs: _StartedProcess(),
    )
    config = DesktopRecordingConfig(
        url="",
        goal="Open the simulator",
        name="launch_simulator",
        asset_path=tmp_path / "launch_simulator.json",
        output_path=tmp_path / "test_launch_simulator.py",
        workspace=tmp_path,
    )
    engine = DesktopRecordingEngine(
        config,
        device=FakeDevice(),
        recorder=FakeRecorder(),
    )
    engine.start()

    snapshot = engine.import_and_launch_tool(executable, ("--recording",))

    assert snapshot.step_count == 1
    step = snapshot.steps[0]
    assert step.action.name == "launch_application"
    assert step.action.arguments["args"] == ["--recording"]
    assert step.verification.is_verified is True
    exported = config.output_path.read_text(encoding="utf-8")
    assert "subprocess.Popen(" in exported
    assert "'--recording'" in exported


def test_desktop_engine_runs_and_records_manual_http_request(
    tmp_path: Path,
) -> None:
    received: list[dict[str, object]] = []

    class _ApiHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append(
                {
                    "path": self.path,
                    "authorization": self.headers["Authorization"],
                    "body": json.loads(body),
                }
            )
            payload = json.dumps({"id": 42}).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args) -> None:  # noqa: ANN001, A002
            return None

    server = ThreadingHTTPServer(("127.0.0.1", 0), _ApiHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    request_url = f"http://127.0.0.1:{server.server_port}/items"
    try:
        config = DesktopRecordingConfig(
            url="",
            goal="Create an item by API",
            name="api_request",
            asset_path=tmp_path / "api_request.json",
            output_path=tmp_path / "test_api_request.py",
            workspace=tmp_path,
        )
        engine = DesktopRecordingEngine(
            config,
            device=FakeDevice(),
            recorder=FakeRecorder(),
        )
        engine.start()

        snapshot = engine.run_http_request(
            {
                "method": "POST",
                "url": request_url,
                "headers": {"Authorization": "Bearer test-token"},
                "json": {"name": "book"},
                "expected_status": 201,
                "timeout": 5,
            }
        )

        generated = runpy.run_path(str(config.output_path))
        generated["test_api_request"]()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert snapshot.step_count == 1
    step = snapshot.steps[0]
    assert step.action.name == "http_request"
    assert step.action.arguments["json"] == {"name": "book"}
    assert step.verification.is_verified is True
    output = step.verification.evidence["output"]
    assert isinstance(output, dict)
    assert output["status"] == 201
    assert output["json"] == {"id": 42}
    assert received == [
        {
            "path": "/items",
            "authorization": "Bearer test-token",
            "body": {"name": "book"},
        },
        {
            "path": "/items",
            "authorization": "Bearer test-token",
            "body": {"name": "book"},
        },
    ]
