import threading
import time

import pytest

from web_e2e_test_studio.agent import (
    ActionRegistry,
    ActionSpec,
    ExecutionRunner,
    PlanCompiler,
    PlannedAction,
    PlanningResult,
    PlanningStatus,
    TaskStatus,
)
from web_e2e_test_studio.agent.control import CancellationToken
from web_e2e_test_studio.agent.lifecycle import (
    ErrorCategory,
    TaskFailure,
    TaskLifecycle,
    TaskPhase,
    TaskStateSnapshot,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    ActionStatus,
    Observation,
    VerificationResult,
    VerificationStatus,
)
from web_e2e_test_studio.recording import RecordingSession


class SequenceObserver:
    def __init__(self) -> None:
        self.index = 0

    def observe(self) -> Observation:
        self.index += 1
        return Observation(url=f"https://example.test/{self.index}")


class SuccessfulExecutor:
    def __init__(self) -> None:
        self.actions: list[Action] = []

    def execute(self, action: Action) -> ActionResult:
        self.actions.append(action)
        return ActionResult.succeeded({"action": action.name})


class SuccessfulVerifier:
    def verify(
        self,
        action: Action,
        result: ActionResult,
        before: Observation,
        after: Observation,
        expected_result: str,
    ) -> VerificationResult:
        return VerificationResult(
            VerificationStatus.VERIFIED,
            expected_result,
            {"before": before.url, "after": after.url},
        )


class TimeoutExecutor:
    def execute(self, action: Action) -> ActionResult:
        raise TimeoutError("Browser action timed out")


class CancellableExecutor:
    def __init__(self) -> None:
        self.started = threading.Event()

    def execute(self, action: Action, control) -> ActionResult:  # noqa: ANN001
        self.started.set()
        while not control.cancelled:
            time.sleep(0.005)
        return control.cancelled_result()


class DeadlineAwareExecutor:
    def execute(self, action: Action, control) -> ActionResult:  # noqa: ANN001
        while not control.timed_out:
            time.sleep(0.002)
        return control.timeout_result("Action deadline exceeded")


class FailingObservation(SequenceObserver):
    def observe(self) -> Observation:
        self.index += 1
        if self.index == 2:
            raise RuntimeError("page closed")
        return Observation(url=f"https://example.test/{self.index}")


def test_task_lifecycle_accepts_only_declared_transitions() -> None:
    lifecycle = TaskLifecycle.create("task_123")

    lifecycle = lifecycle.transition(TaskPhase.RUNNING)
    lifecycle = lifecycle.transition(TaskPhase.VERIFYING)
    lifecycle = lifecycle.transition(TaskPhase.SUCCEEDED)

    assert lifecycle.phase is TaskPhase.SUCCEEDED
    assert [item.phase for item in lifecycle.transitions] == [
        TaskPhase.QUEUED,
        TaskPhase.RUNNING,
        TaskPhase.VERIFYING,
        TaskPhase.SUCCEEDED,
    ]
    with pytest.raises(ValueError, match="terminal"):
        lifecycle.transition(TaskPhase.RUNNING)


def test_task_lifecycle_rejects_skipping_execution_states() -> None:
    lifecycle = TaskLifecycle.create("task_123")

    with pytest.raises(ValueError, match="queued.*succeeded"):
        lifecycle.transition(TaskPhase.SUCCEEDED)

    with pytest.raises(ValueError, match="must begin in queued"):
        TaskLifecycle("task_123", phase=TaskPhase.SUCCEEDED)


def test_task_state_snapshot_rejects_invalid_persisted_history() -> None:
    with pytest.raises(ValueError, match="Invalid task transition"):
        TaskStateSnapshot.from_dict(
            {
                "lifecycle": {
                    "task_id": "task_123",
                    "phase": "succeeded",
                    "transitions": [
                        {"phase": "queued", "at": "one"},
                        {"phase": "succeeded", "at": "two"},
                    ],
                },
                "failure": None,
            }
        )


@pytest.mark.parametrize(
    ("category", "retryable", "refresh_context", "replan", "ask_user"),
    [
        (ErrorCategory.PROTOCOL, True, False, True, False),
        (ErrorCategory.LOCATE, True, True, True, False),
        (ErrorCategory.AMBIGUOUS, True, True, True, False),
        (ErrorCategory.NAVIGATION, True, True, True, False),
        (ErrorCategory.EXECUTION, False, False, True, False),
        (ErrorCategory.VERIFICATION, True, True, True, False),
        (ErrorCategory.CAPABILITY, False, False, False, True),
        (ErrorCategory.INFRASTRUCTURE, True, False, False, False),
        (ErrorCategory.TIMEOUT, True, False, True, False),
        (ErrorCategory.CANCELLED, False, False, False, False),
    ],
)
def test_task_failure_exposes_recovery_policy(
    category: ErrorCategory,
    retryable: bool,
    refresh_context: bool,
    replan: bool,
    ask_user: bool,
) -> None:
    failure = TaskFailure.create(
        category,
        code="test_failure",
        message="Something failed",
    )

    assert failure.retryable is retryable
    assert failure.recovery.refresh_context is refresh_context
    assert failure.recovery.replan is replan
    assert failure.recovery.ask_user is ask_user
    assert TaskFailure.from_dict(failure.to_dict()) == failure


def test_task_failure_classifies_deterministic_action_results() -> None:
    timeout = TaskFailure.from_action_result(
        ActionResult(ActionStatus.TIMEOUT, error="Timed out")
    )
    blocked = TaskFailure.from_action_result(
        ActionResult(ActionStatus.BLOCKED, error="Tool is not registered")
    )

    assert timeout.category is ErrorCategory.TIMEOUT
    assert blocked.category is ErrorCategory.CAPABILITY


def _registry(executor: SuccessfulExecutor) -> ActionRegistry:
    registry = ActionRegistry()
    for name in ("click", "wait"):
        registry.register(
            ActionSpec(
                name=name,
                channel=ActionChannel.BROWSER,
                description=name,
            ),
            executor,
        )
    return registry


def test_planning_result_requires_exactly_one_terminal_shape() -> None:
    action = PlannedAction(
        action=Action("wait", ActionChannel.BROWSER),
        intent="Wait",
        expected_result="Page settles",
    )

    assert PlanningResult.actions((action,)).status is PlanningStatus.ACTIONS
    assert PlanningResult.ask("Which account?").status is PlanningStatus.ASK
    assert PlanningResult.complete("Finished").status is PlanningStatus.COMPLETE


def test_plan_compiler_validates_all_actions_before_execution() -> None:
    executor = SuccessfulExecutor()
    compiler = PlanCompiler(_registry(executor))
    plan = PlanningResult.actions(
        (
            PlannedAction(
                Action("click", ActionChannel.BROWSER),
                "Open",
                "Opened",
            ),
            PlannedAction(
                Action("wait", ActionChannel.BROWSER),
                "Wait",
                "Settled",
            ),
        )
    )

    tasks = compiler.compile(plan)

    assert [task.action.name for task in tasks] == ["click", "wait"]
    assert executor.actions == []


def test_plan_compiler_rejects_argument_types_before_any_execution() -> None:
    executor = SuccessfulExecutor()
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            "http_request",
            ActionChannel.API,
            "HTTP",
            required_arguments=("url", "expected_status"),
            argument_schema={"url": "string", "expected_status": "integer"},
        ),
        executor,
    )
    plan = PlanningResult.actions(
        (
            PlannedAction(
                Action(
                    "http_request",
                    ActionChannel.API,
                    {"url": "https://example.test", "expected_status": "200"},
                ),
                "Request",
                "Responds",
            ),
        )
    )

    with pytest.raises(ValueError, match="expected_status.*integer"):
        PlanCompiler(registry).compile(plan)

    assert executor.actions == []


def test_execution_runner_creates_structured_dump_and_verified_steps() -> None:
    executor = SuccessfulExecutor()
    registry = _registry(executor)
    tasks = PlanCompiler(registry).compile(
        PlanningResult.actions(
            (
                PlannedAction(
                    Action("click", ActionChannel.BROWSER),
                    "Open menu",
                    "Menu opens",
                ),
                PlannedAction(
                    Action("wait", ActionChannel.BROWSER),
                    "Wait for menu",
                    "Menu settles",
                ),
            )
        )
    )
    session = RecordingSession.create(name="menu", goal="Open menu")

    execution = ExecutionRunner(
        observer=SequenceObserver(),
        verifier=SuccessfulVerifier(),
        actions=registry,
    ).run(session, tasks)

    assert execution.status is TaskStatus.SUCCEEDED
    assert [record.status for record in execution.records] == [
        TaskStatus.SUCCEEDED,
        TaskStatus.SUCCEEDED,
    ]
    assert [step.intent for step in execution.steps] == [
        "Open menu",
        "Wait for menu",
    ]
    payload = execution.to_dict()
    assert payload["records"][0]["before"]["url"] == "https://example.test/1"
    assert payload["records"][1]["after"]["url"] == "https://example.test/3"
    assert payload["records"][0]["lifecycle"]["phase"] == "succeeded"
    assert [
        item["phase"]
        for item in payload["records"][0]["lifecycle"]["transitions"]
    ] == ["queued", "running", "verifying", "succeeded"]
    assert payload["records"][0]["failure"] is None
    assert session.asset.execution_history == [payload]


def test_execution_runner_records_failure_and_cancels_remaining_tasks() -> None:
    executor = SuccessfulExecutor()
    registry = _registry(executor)
    tasks = PlanCompiler(registry).compile(
        PlanningResult.actions(
            (
                PlannedAction(
                    Action("click", ActionChannel.BROWSER),
                    "Click",
                    "Clicked",
                ),
                PlannedAction(
                    Action("wait", ActionChannel.BROWSER),
                    "Wait",
                    "Settled",
                ),
            )
        )
    )
    session = RecordingSession.create(name="failure", goal="Fail safely")

    execution = ExecutionRunner(
        observer=FailingObservation(),
        verifier=SuccessfulVerifier(),
        actions=registry,
    ).run(session, tasks)

    assert execution.status is TaskStatus.FAILED
    assert [record.status for record in execution.records] == [
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    ]
    assert "Post-action observation failed" in execution.records[0].result.error
    assert (
        execution.records[0].failure.category
        is ErrorCategory.NAVIGATION
    )
    assert execution.records[0].lifecycle.phase is TaskPhase.FAILED
    assert execution.records[1].failure.category is ErrorCategory.CANCELLED
    assert execution.records[1].lifecycle.phase is TaskPhase.CANCELLED
    assert session.pending == ()


def test_execution_runner_preserves_timeout_exception_category() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec("wait", ActionChannel.BROWSER, "Wait"),
        TimeoutExecutor(),
    )
    task = PlanCompiler(registry).compile(
        PlanningResult.actions(
            (
                PlannedAction(
                    Action("wait", ActionChannel.BROWSER),
                    "Wait",
                    "Settled",
                ),
            )
        )
    )

    execution = ExecutionRunner(
        verifier=SuccessfulVerifier(),
        actions=registry,
    ).run(RecordingSession.create(name="timeout", goal="Wait"), task)

    assert execution.records[0].failure.category is ErrorCategory.TIMEOUT
    assert execution.records[0].failure.retryable is True


def test_execution_runner_cancels_active_and_remaining_actions() -> None:
    executor = CancellableExecutor()
    registry = ActionRegistry()
    for name in ("first", "second"):
        registry.register(
            ActionSpec(name, ActionChannel.EXTERNAL, name),
            executor,
        )
    tasks = PlanCompiler(registry).compile(
        PlanningResult.actions(
            tuple(
                PlannedAction(
                    Action(name, ActionChannel.EXTERNAL),
                    name,
                    f"{name} completes",
                )
                for name in ("first", "second")
            )
        )
    )
    cancellation = CancellationToken()
    result: list = []
    worker = threading.Thread(
        target=lambda: result.append(
            ExecutionRunner(
                verifier=SuccessfulVerifier(),
                actions=registry,
                cancellation=cancellation,
            ).run(
                RecordingSession.create(name="cancel", goal="Cancel safely"),
                tasks,
            )
        )
    )

    worker.start()
    assert executor.started.wait(1)
    cancellation.cancel("Cancelled by user")
    worker.join(1)

    assert not worker.is_alive()
    assert [record.status for record in result[0].records] == [
        TaskStatus.CANCELLED,
        TaskStatus.CANCELLED,
    ]
    assert result[0].records[0].failure.category is ErrorCategory.CANCELLED
    assert result[0].steps == ()


def test_execution_runner_enforces_action_deadline_and_stops_batch() -> None:
    registry = ActionRegistry()
    executor = DeadlineAwareExecutor()
    for name in ("slow", "later"):
        registry.register(
            ActionSpec(name, ActionChannel.EXTERNAL, name),
            executor,
        )
    tasks = PlanCompiler(registry).compile(
        PlanningResult.actions(
            (
                PlannedAction(
                    Action(
                        "slow",
                        ActionChannel.EXTERNAL,
                        timeout_seconds=0.02,
                    ),
                    "Run slow action",
                    "Slow action completes",
                ),
                PlannedAction(
                    Action("later", ActionChannel.EXTERNAL),
                    "Run later action",
                    "Later action completes",
                ),
            )
        )
    )

    execution = ExecutionRunner(
        verifier=SuccessfulVerifier(),
        actions=registry,
    ).run(
        RecordingSession.create(name="deadline", goal="Bound action"),
        tasks,
    )

    assert execution.records[0].result.status is ActionStatus.TIMEOUT
    assert execution.records[0].failure.category is ErrorCategory.TIMEOUT
    assert execution.records[1].status is TaskStatus.CANCELLED
    assert execution.steps == ()


def test_persisted_execution_dump_redacts_nested_secrets() -> None:
    executor = SuccessfulExecutor()
    registry = _registry(executor)
    task = PlanCompiler(registry).compile(
        PlanningResult.actions(
            (
                PlannedAction(
                    Action(
                        "wait",
                        ActionChannel.BROWSER,
                        arguments={
                            "headers": {
                                "Authorization": "Bearer project-secret"
                            }
                        },
                    ),
                    "Wait",
                    "Settled",
                ),
            )
        )
    )
    session = RecordingSession.create(name="safe", goal="Do not leak")

    ExecutionRunner(
        verifier=SuccessfulVerifier(),
        actions=registry,
    ).run(session, task)

    persisted = str(session.asset.execution_history)
    assert "project-secret" not in persisted
    assert "[REDACTED]" in persisted
