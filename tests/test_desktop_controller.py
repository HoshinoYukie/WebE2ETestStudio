from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

from web_e2e_test_studio.browser import BrowserPageInfo
from web_e2e_test_studio.desktop.controller import (
    DesktopStudioController,
    format_studio_error,
)
from web_e2e_test_studio.desktop.engine import (
    DesktopRecordingConfig,
    DesktopRecordingSnapshot,
)
from web_e2e_test_studio.desktop.validation import ModelFreeValidationResult
from web_e2e_test_studio.domain import Observation


class FakeEngine:
    def __init__(self, config: DesktopRecordingConfig) -> None:
        self.config = config
        self.calls: list[str] = []
        self.running = False
        self.ai_started = threading.Event()
        self.cancelled = threading.Event()

    def start(self) -> DesktopRecordingSnapshot:
        self.calls.append("start")
        self.running = True
        return self._snapshot()

    def resume(self) -> DesktopRecordingSnapshot:
        self.calls.append("resume")
        self.running = True
        return self._snapshot()

    def sync_manual(self) -> DesktopRecordingSnapshot:
        self.calls.append("sync_manual")
        return self._snapshot()

    def run_ai(self, instruction, event_sink=None):  # noqa: ANN001
        self.calls.append("run_ai")
        self.ai_started.set()
        self.cancelled.wait(1)
        return SimpleNamespace(status=SimpleNamespace(value="cancelled")), self._snapshot()

    def cancel(self) -> bool:
        self.calls.append("cancel")
        self.cancelled.set()
        return True

    def preview(self) -> DesktopRecordingSnapshot:
        self.calls.append("preview")
        return self._snapshot()

    def activate_page(self, page_id: str) -> DesktopRecordingSnapshot:
        self.calls.append(f"activate_page:{page_id}")
        return self._snapshot()

    def save(self) -> DesktopRecordingSnapshot:
        self.calls.append("save")
        return self._snapshot()

    def validate_export(self):  # noqa: ANN201
        self.calls.append("validate_export")
        return (
            ModelFreeValidationResult(
                passed=True,
                exit_code=0,
                duration_seconds=0.25,
                output="1 passed",
                test_path=self.config.output_path,
            ),
            self._snapshot(),
        )

    def stop(self) -> DesktopRecordingSnapshot:
        self.calls.append("stop")
        self.running = False
        return self._snapshot()

    def _snapshot(self) -> DesktopRecordingSnapshot:
        return DesktopRecordingSnapshot(
            running=self.running,
            steps=(),
            last_observation=Observation(title="Ready"),
            asset_path=self.config.asset_path,
            output_path=self.config.output_path,
            pages=(
                BrowserPageInfo(
                    id="page_1",
                    url="https://example.test/live",
                    title="Live page",
                    active=True,
                ),
            ),
            active_page_id="page_1",
        )


def test_controller_runs_commands_on_worker_and_emits_events(
    tmp_path: Path,
) -> None:
    engines: list[FakeEngine] = []

    def factory(config: DesktopRecordingConfig) -> FakeEngine:
        engine = FakeEngine(config)
        engines.append(engine)
        return engine

    config = DesktopRecordingConfig(
        url="https://example.test",
        goal="Test desktop",
        name="desktop",
        asset_path=tmp_path / "asset.json",
        output_path=tmp_path / "test_desktop.py",
        workspace=tmp_path,
    )
    controller = DesktopStudioController(engine_factory=factory)
    try:
        controller.start(config)
        started = _wait_for_event(controller, "snapshot")
        controller.sync_manual()
        synced = _wait_for_event(controller, "snapshot")
        controller.validate_export()
        validated = _wait_for_event(controller, "validation_passed")

        assert started.snapshot is not None
        assert started.snapshot.running is True
        assert synced.message == "Manual browser actions synchronized"
        assert validated.payload["status"] == "passed"
        assert validated.payload["output"] == "1 passed"
        assert engines[0].calls == ["start", "sync_manual", "validate_export"]
    finally:
        controller.close()


def test_controller_resumes_a_recoverable_session(tmp_path: Path) -> None:
    engines: list[FakeEngine] = []

    def factory(config: DesktopRecordingConfig) -> FakeEngine:
        engine = FakeEngine(config)
        engines.append(engine)
        return engine

    config = DesktopRecordingConfig(
        url="https://example.test",
        goal="Resume",
        name="resume",
        asset_path=tmp_path / "resume.json",
        output_path=tmp_path / "test_resume.py",
        workspace=tmp_path,
    )
    controller = DesktopStudioController(engine_factory=factory)
    try:
        controller.resume(config)
        event = _wait_for_event(controller, "snapshot")

        assert event.message == "Recording resumed"
        assert engines[0].calls == ["resume"]
    finally:
        controller.close()


def test_controller_streams_preview_and_switches_tabs(tmp_path: Path) -> None:
    engines: list[FakeEngine] = []

    def factory(config: DesktopRecordingConfig) -> FakeEngine:
        engine = FakeEngine(config)
        engines.append(engine)
        return engine

    controller = DesktopStudioController(
        engine_factory=factory,
        preview_interval=0.02,
    )
    try:
        controller.start(
            DesktopRecordingConfig(
                url="",
                goal="Preview tabs",
                name="preview",
                asset_path=tmp_path / "preview.json",
                output_path=tmp_path / "test_preview.py",
                workspace=tmp_path,
            )
        )
        _wait_for_event(controller, "snapshot")
        preview = _wait_for_event(controller, "browser_preview")

        assert preview.snapshot is not None
        assert preview.snapshot.pages[0].id == "page_1"
        controller.activate_page("page_1")
        switched = _wait_for_event(controller, "browser_preview")

        assert switched.snapshot is not None
        assert "activate_page:page_1" in engines[0].calls
    finally:
        controller.close()


def test_controller_refreshes_preview_and_switches_tabs_on_worker(
    tmp_path: Path,
) -> None:
    engines: list[FakeEngine] = []

    def factory(config: DesktopRecordingConfig) -> FakeEngine:
        engine = FakeEngine(config)
        engines.append(engine)
        return engine

    config = DesktopRecordingConfig(
        url="",
        goal="Monitor tabs",
        name="tabs",
        asset_path=tmp_path / "tabs.json",
        output_path=tmp_path / "test_tabs.py",
        workspace=tmp_path,
    )
    controller = DesktopStudioController(engine_factory=factory)
    try:
        controller.start(config)
        _wait_for_event(controller, "snapshot")

        preview = _wait_for_event(controller, "browser_preview")
        controller.activate_page("page_002")
        switched = _wait_for_event(controller, "browser_preview")

        assert preview.message == "Live browser frame"
        assert switched.message == "Browser tab activated"
        assert engines[0].calls == [
            "start",
            "preview",
            "activate_page:page_002",
        ]
    finally:
        controller.close()


def test_controller_coalesces_unconsumed_preview_frames(
    tmp_path: Path,
) -> None:
    controller = DesktopStudioController(
        engine_factory=FakeEngine,
        preview_interval=0.01,
    )
    try:
        controller.start(
            DesktopRecordingConfig(
                url="",
                goal="Bound preview frames",
                name="bounded_preview",
                asset_path=tmp_path / "preview.json",
                output_path=tmp_path / "test_preview.py",
                workspace=tmp_path,
            )
        )
        _wait_for_event(controller, "snapshot")
        time.sleep(0.08)

        events = controller.poll_events()

        assert sum(
            event.kind == "browser_preview" for event in events
        ) <= 1
    finally:
        controller.close()


def test_controller_interrupts_active_action_without_waiting_for_worker(
    tmp_path: Path,
) -> None:
    engines: list[FakeEngine] = []

    def factory(config: DesktopRecordingConfig) -> FakeEngine:
        engine = FakeEngine(config)
        engines.append(engine)
        return engine

    config = DesktopRecordingConfig(
        url="",
        goal="Cancelable run",
        name="cancel",
        asset_path=tmp_path / "cancel.json",
        output_path=tmp_path / "test_cancel.py",
        workspace=tmp_path,
    )
    controller = DesktopStudioController(engine_factory=factory)
    try:
        controller.start(config)
        _wait_for_event(controller, "snapshot")
        controller.run_ai("Run a slow tool")
        assert engines[0].ai_started.wait(1)

        assert controller.cancel() is True
        finished = _wait_for_event(controller, "snapshot")

        assert finished.message == "VLM run finished: cancelled"
        assert engines[0].calls[-2:] == ["run_ai", "cancel"]
    finally:
        controller.close()


def _wait_for_event(
    controller: DesktopStudioController, kind: str
):  # noqa: ANN201
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        for event in controller.poll_events():
            if event.kind == kind:
                return event
        time.sleep(0.01)
    raise AssertionError(f"Timed out waiting for {kind}")


def test_studio_error_hides_provider_html_permission_page() -> None:
    PermissionDeniedError = type("PermissionDeniedError", (Exception,), {})
    message = format_studio_error(
        PermissionDeniedError(
            "<!-- IE friendly error message workaround. -->"
            "<html><body>Access denied</body></html>"
        )
    )

    assert "<html" not in message
    assert "VLM request was denied" in message
    assert "Ignore system proxy" in message
