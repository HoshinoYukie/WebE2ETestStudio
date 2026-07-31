from __future__ import annotations

from pathlib import Path

from test_desktop_engine import FakeDevice, FakeRecorder

from web_e2e_test_studio.agent import (
    ErrorCategory,
    PlannedAction,
    PlanningResult,
    TaskFailure,
    TaskLifecycle,
    TaskPhase,
    TaskStateSnapshot,
)
from web_e2e_test_studio.desktop.engine import (
    DesktopRecordingConfig,
    DesktopRecordingEngine,
)
from web_e2e_test_studio.desktop.recovery import (
    RunCheckpoint,
    RunCheckpointStore,
)
from web_e2e_test_studio.domain import Action, ActionChannel, Locator
from web_e2e_test_studio.storage import JsonAssetStore


class _AskPlanner:
    def plan(self, request, observation, history, actions):  # noqa: ANN001, ANN201
        return PlanningResult.ask("Which worksheet should be used?")


class _MemoryAskPlanner:
    def __init__(self) -> None:
        self.memories: tuple[str, ...] = ()

    def set_memories(self, memories) -> None:  # noqa: ANN001
        self.memories = tuple(memories)

    def plan(self, request, observation, history, actions):  # noqa: ANN001, ANN201
        if self.memories:
            return PlanningResult.complete(f"Recovered: {self.memories[0]}")
        return PlanningResult.ask(
            "Which worksheet should be used?",
            memory="Downloaded workbook path is C:\\downloads\\devices.xlsx",
        )


class _ActionThenCompletePlanner:
    def __init__(self) -> None:
        self.calls = 0

    def plan(self, request, observation, history, actions):  # noqa: ANN001, ANN201
        self.calls += 1
        if self.calls > 1:
            return PlanningResult.complete("Clicked")
        return PlanningResult.actions(
            (
                PlannedAction(
                    Action(
                        "click",
                        ActionChannel.BROWSER,
                        locator=Locator("role", "button", name="Continue"),
                    ),
                    "Continue",
                    "The next view opens",
                ),
            )
        )


def test_checkpoint_store_atomically_discovers_and_loads_runs(
    tmp_path: Path,
) -> None:
    store = RunCheckpointStore(tmp_path)
    checkpoint = RunCheckpoint.create(
        name="devices",
        goal="Export devices",
        asset_path=tmp_path / "recordings" / "devices.json",
        output_path=tmp_path / "recordings" / "test_devices.py",
        workspace=tmp_path,
        initial_url="https://example.test/devices",
        headless=False,
    ).updated(
        state="waiting_for_user",
        active_instruction="Export and inspect the file",
        pending_question="Which sheet?",
        round_number=3,
        last_url="https://example.test/devices",
        task_lifecycles={
            "task_123": TaskStateSnapshot(
                TaskLifecycle.create("task_123")
                .transition(TaskPhase.RUNNING)
                .transition(TaskPhase.FAILED),
                TaskFailure.create(
                    ErrorCategory.NAVIGATION,
                    code="post_action_observation_failed",
                    message="Page navigated",
                ),
            )
        },
    )

    path = store.save(checkpoint)
    checkpoint.asset_path.write_text("{}", encoding="utf-8")
    discovered = store.discover()

    assert path.name == "devices.run.json"
    assert discovered == (checkpoint,)
    assert store.load(path) == checkpoint


def test_desktop_engine_resumes_verified_asset_and_last_page(
    tmp_path: Path,
) -> None:
    config = DesktopRecordingConfig(
        url="https://example.test/start",
        goal="Resume this test",
        name="resume_flow",
        asset_path=tmp_path / "recordings" / "resume_flow.json",
        output_path=tmp_path / "recordings" / "test_resume_flow.py",
        workspace=tmp_path,
    )
    first_device = FakeDevice()
    first = DesktopRecordingEngine(
        config,
        device=first_device,
        recorder=FakeRecorder(),
    )
    started = first.start()
    first.stop()

    checkpoint_path = config.asset_path.with_suffix(".run.json")
    assert checkpoint_path.exists()
    assert started.step_count == 1

    resumed_device = FakeDevice()
    resumed = DesktopRecordingEngine(
        config,
        device=resumed_device,
        recorder=FakeRecorder(),
    ).resume()

    assert resumed.running is True
    assert resumed.step_count == 1
    assert resumed_device.started is True
    assert resumed_device.url == "https://example.test/start"
    restored = JsonAssetStore().load(config.asset_path)
    assert len(restored.steps) == 1


def test_checkpoint_does_not_persist_secret_event_values(
    tmp_path: Path,
) -> None:
    checkpoint = RunCheckpoint.create(
        name="safe",
        goal="Call API",
        asset_path=tmp_path / "safe.json",
        output_path=tmp_path / "test_safe.py",
        workspace=tmp_path,
        initial_url="",
        headless=True,
    ).with_event(
        "tool_result",
        {"api_key": "project-secret", "message": "done"},
    )

    path = RunCheckpointStore(tmp_path).save(checkpoint)
    content = path.read_text(encoding="utf-8")

    assert "project-secret" not in content
    assert "[REDACTED]" in content


def test_waiting_agent_question_is_recoverable(tmp_path: Path) -> None:
    config = DesktopRecordingConfig(
        url="",
        goal="Inspect workbook",
        name="waiting",
        asset_path=tmp_path / "recordings" / "waiting.json",
        output_path=tmp_path / "recordings" / "test_waiting.py",
        workspace=tmp_path,
    )
    engine = DesktopRecordingEngine(
        config,
        device=FakeDevice(),
        recorder=FakeRecorder(),
        planner_factory=_AskPlanner,
    )
    engine.start()

    result, _ = engine.run_ai("Open and inspect the downloaded workbook")
    checkpoint = RunCheckpointStore(tmp_path).load(
        config.asset_path.with_suffix(".run.json")
    )

    assert result.question == "Which worksheet should be used?"
    assert checkpoint.state == "waiting_for_user"
    assert checkpoint.active_instruction == (
        "Open and inspect the downloaded workbook"
    )
    assert checkpoint.pending_question == "Which worksheet should be used?"
    assert any(
        event["kind"] == "question" for event in checkpoint.events
    )


def test_working_memory_is_checkpointed_and_reused_after_user_reply(
    tmp_path: Path,
) -> None:
    config = DesktopRecordingConfig(
        url="",
        goal="Inspect workbook",
        name="memory",
        asset_path=tmp_path / "recordings" / "memory.json",
        output_path=tmp_path / "recordings" / "test_memory.py",
        workspace=tmp_path,
    )
    engine = DesktopRecordingEngine(
        config,
        device=FakeDevice(),
        recorder=FakeRecorder(),
        planner_factory=_MemoryAskPlanner,
    )
    engine.start()

    first, _ = engine.run_ai("Inspect the downloaded workbook")
    checkpoint = RunCheckpointStore(tmp_path).load(
        config.asset_path.with_suffix(".run.json")
    )
    second, _ = engine.run_ai("Use the first worksheet")

    assert first.status.value == "waiting_for_user"
    assert checkpoint.memories == (
        "Downloaded workbook path is C:\\downloads\\devices.xlsx",
    )
    assert second.summary.startswith("Recovered:")


def test_desktop_checkpoint_tracks_completed_task_lifecycle(
    tmp_path: Path,
) -> None:
    config = DesktopRecordingConfig(
        url="",
        goal="Continue",
        name="lifecycle",
        asset_path=tmp_path / "recordings" / "lifecycle.json",
        output_path=tmp_path / "recordings" / "test_lifecycle.py",
        workspace=tmp_path,
    )
    engine = DesktopRecordingEngine(
        config,
        device=FakeDevice(),
        recorder=FakeRecorder(),
        planner_factory=_ActionThenCompletePlanner,
    )
    engine.start()

    result, _ = engine.run_ai("Click Continue")
    checkpoint = RunCheckpointStore(tmp_path).load(
        config.asset_path.with_suffix(".run.json")
    )

    assert result.status.value == "completed"
    task_state = next(iter(checkpoint.task_lifecycles.values()))
    assert task_state.lifecycle.phase is TaskPhase.SUCCEEDED
    assert task_state.failure is None
