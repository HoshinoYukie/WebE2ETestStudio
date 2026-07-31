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
    TaskStatus,
)
from web_e2e_test_studio.agent.control import (
    ActionCancelledError,
    ActionExecutionContext,
    ActionTimeoutError,
    CancellationToken,
)
from web_e2e_test_studio.browser.playwright import PlaywrightDevice
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    VerificationResult,
    VerificationStatus,
)
from web_e2e_test_studio.recording import RecordingSession


def test_cancellation_token_is_idempotent_and_wakes_waiters() -> None:
    token = CancellationToken()
    awakened = threading.Event()

    waiter = threading.Thread(
        target=lambda: (token.wait(), awakened.set()),
        daemon=True,
    )
    waiter.start()
    token.cancel("User stopped the run")
    token.cancel("A later reason must not replace the first")

    assert awakened.wait(1)
    assert token.cancelled is True
    assert token.reason == "User stopped the run"
    with pytest.raises(ActionCancelledError, match="User stopped"):
        token.raise_if_cancelled()


def test_execution_context_enforces_deadline_and_runs_cancel_callbacks() -> None:
    token = CancellationToken()
    callback_called = threading.Event()
    context = ActionExecutionContext.create(
        token=token,
        timeout_ms=10,
    )
    remove_callback = token.add_cancel_callback(callback_called.set)

    time.sleep(0.02)

    with pytest.raises(ActionTimeoutError):
        context.raise_if_stopped()
    token.cancel()
    assert callback_called.is_set()
    remove_callback()


class _ContextAwareExecutor:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.executed: list[str] = []

    def execute(
        self,
        action: Action,
        context: ActionExecutionContext | None = None,
    ) -> ActionResult:
        if context is None:
            raise AssertionError("Execution context was not supplied")
        self.started.set()
        while True:
            context.raise_if_stopped()
            self.executed.append(action.name)
            time.sleep(0.005)


class _Verifier:
    def verify(self, action, result, before, after, expected_result):  # noqa: ANN001, ANN201
        return VerificationResult(
            VerificationStatus.VERIFIED,
            expected_result,
        )


def _tasks(
    registry: ActionRegistry, count: int = 1
):  # noqa: ANN202
    return PlanCompiler(registry).compile(
        PlanningResult.actions(
            tuple(
                PlannedAction(
                    Action(
                        "work",
                        ActionChannel.EXTERNAL,
                        timeout_seconds=(
                            0.02 if count == 1 else 5
                        ),
                    ),
                    f"Work {index}",
                    "Done",
                )
                for index in range(count)
            )
        )
    )


def test_execution_runner_times_out_one_action_at_its_deadline() -> None:
    executor = _ContextAwareExecutor()
    registry = ActionRegistry()
    registry.register(
        ActionSpec("work", ActionChannel.EXTERNAL, "Work"),
        executor,
    )

    execution = ExecutionRunner(
        verifier=_Verifier(),
        actions=registry,
    ).run(
        RecordingSession.create(name="timeout", goal="Work"),
        _tasks(registry),
    )

    assert execution.records[0].status is TaskStatus.FAILED
    assert execution.records[0].failure.category.value == "timeout"
    assert execution.steps == ()


def test_execution_runner_cancels_active_and_remaining_actions() -> None:
    executor = _ContextAwareExecutor()
    registry = ActionRegistry()
    registry.register(
        ActionSpec("work", ActionChannel.EXTERNAL, "Work"),
        executor,
    )
    token = CancellationToken()
    session = RecordingSession.create(name="cancel", goal="Work")
    outcome = []
    worker = threading.Thread(
        target=lambda: outcome.append(
                ExecutionRunner(
                    verifier=_Verifier(),
                    actions=registry,
                    cancellation=token,
                ).run(
                    session,
                    _tasks(registry, 2),
                )
        ),
        daemon=True,
    )
    worker.start()
    assert executor.started.wait(1)

    token.cancel("Stopped by user")
    worker.join(1)

    assert worker.is_alive() is False
    assert outcome[0].status is TaskStatus.CANCELLED
    assert [record.status for record in outcome[0].records] == [
        TaskStatus.CANCELLED,
        TaskStatus.CANCELLED,
    ]
    assert session.asset.steps == []


def test_playwright_wait_checks_cancellation_in_short_slices() -> None:
    token = CancellationToken()

    class Page:
        context = None

        def __init__(self) -> None:
            self.waits = []

        def wait_for_timeout(self, milliseconds: float) -> None:
            self.waits.append(milliseconds)
            token.cancel("Stopped by user")

    page = Page()
    result = PlaywrightDevice(page=page).execute(
        Action(
            "wait",
            ActionChannel.BROWSER,
            arguments={"milliseconds": 10_000},
        ),
        ActionExecutionContext.create(token=token, timeout_ms=20_000),
    )

    assert result.status.value == "cancelled"
    assert page.waits[0] <= 100
