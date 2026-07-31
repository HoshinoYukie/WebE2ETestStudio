"""Unified deterministic execution, verification, and audit recording."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from web_e2e_test_studio.agent.actions import ActionRegistry
from web_e2e_test_studio.agent.control import (
    ActionExecutionContext,
    CancellationToken,
)
from web_e2e_test_studio.agent.lifecycle import (
    ErrorCategory,
    TaskFailure,
    TaskLifecycle,
    TaskPhase,
    classify_exception,
)
from web_e2e_test_studio.agent.planning import ExecutionTask
from web_e2e_test_studio.context import (
    ContextPurpose,
    ContextRequest,
    capture_observation,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionResult,
    ActionStatus,
    JsonValue,
    Observation,
    RecordedStep,
    StepSource,
    VerificationResult,
    VerificationStatus,
    utc_now,
)
from web_e2e_test_studio.recording import RecordingSession


class TaskStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Observer(Protocol):
    def observe(self) -> Observation: ...


class Verifier(Protocol):
    def verify(
        self,
        action: Action,
        result: ActionResult,
        before: Observation,
        after: Observation,
        expected_result: str,
    ) -> VerificationResult: ...


@dataclass(frozen=True)
class ExecutionRecord:
    task: ExecutionTask
    status: TaskStatus
    before: Observation
    after: Observation
    result: ActionResult
    verification: VerificationResult
    lifecycle: TaskLifecycle | None = None
    failure: TaskFailure | None = None
    step_id: str = ""
    started_at: str = field(default_factory=utc_now)
    finished_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if self.lifecycle is not None:
            return
        lifecycle = TaskLifecycle.create(self.task.id)
        if self.status is TaskStatus.CANCELLED:
            lifecycle = lifecycle.transition(TaskPhase.CANCELLED)
        else:
            lifecycle = lifecycle.transition(TaskPhase.RUNNING).transition(
                TaskPhase.VERIFYING
            )
            lifecycle = lifecycle.transition(
                TaskPhase.SUCCEEDED
                if self.status is TaskStatus.SUCCEEDED
                else TaskPhase.FAILED
            )
        object.__setattr__(self, "lifecycle", lifecycle)

    def to_dict(self) -> dict[str, JsonValue]:
        if self.lifecycle is None:
            raise RuntimeError("Execution record has no task lifecycle")
        return {
            "task_id": self.task.id,
            "action": self.task.action.to_dict(),
            "intent": self.task.intent,
            "expected_result": self.task.expected_result,
            "status": self.status.value,
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "result": self.result.to_dict(),
            "verification": self.verification.to_dict(),
            "lifecycle": self.lifecycle.to_dict(),
            "failure": self.failure.to_dict() if self.failure else None,
            "step_id": self.step_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


@dataclass(frozen=True)
class ExecutionDump:
    status: TaskStatus
    records: tuple[ExecutionRecord, ...]
    steps: tuple[RecordedStep, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "status": self.status.value,
            "records": [record.to_dict() for record in self.records],
            "steps": [step.to_dict() for step in self.steps],
        }

    def to_persisted_dict(self) -> dict[str, JsonValue]:
        """Return a bounded, secret-redacted representation for project assets."""
        sanitized = _sanitize(self.to_dict())
        if not isinstance(sanitized, dict):
            raise RuntimeError("Execution dump sanitization changed its shape")
        return sanitized


class ExecutionRunner:
    """Execute compiled tasks through one auditable workflow."""

    def __init__(
        self,
        *,
        observer: Observer | None = None,
        verifier: Verifier,
        actions: ActionRegistry,
        record_sink: Callable[[ExecutionRecord], None] | None = None,
        cancellation: CancellationToken | None = None,
    ) -> None:
        self._observer = observer
        self._verifier = verifier
        self._actions = actions
        self._record_sink = record_sink
        self._cancellation = cancellation or CancellationToken()

    def run(
        self,
        session: RecordingSession,
        tasks: tuple[ExecutionTask, ...],
        *,
        source: StepSource = StepSource.VLM,
        initial_observation: Observation | None = None,
    ) -> ExecutionDump:
        records: list[ExecutionRecord] = []
        steps: list[RecordedStep] = []
        current_observation = initial_observation
        for task_index, task in enumerate(tasks):
            if self._cancellation.cancelled:
                self._append_cancelled(
                    records,
                    tasks[task_index:],
                    current_observation or Observation(),
                    "cancelled_before_execution",
                    self._cancellation.reason,
                )
                break
            started_at = utc_now()
            lifecycle = TaskLifecycle.create(task.id).transition(
                TaskPhase.RUNNING
            )
            failure: TaskFailure | None = None
            stage = "pre_observation"
            candidate = session.stage(
                task.action,
                intent=task.intent,
                expected_result=task.expected_result,
                source=source,
            )
            before = current_observation or Observation()
            try:
                if current_observation is None:
                    before = self._observe()
                stage = "execution"
                control = ActionExecutionContext.create(
                    self._cancellation,
                    _action_timeout_seconds(task.action),
                )
                result = self._actions.execute(
                    task.action,
                    control,
                    allow_user_initiated=source is StepSource.USER,
                )
                if result.status is ActionStatus.CANCELLED:
                    after = before
                    verification = VerificationResult(
                        VerificationStatus.INCONCLUSIVE,
                        "The action was cancelled",
                    )
                    lifecycle = lifecycle.transition(TaskPhase.CANCELLED)
                    failure = TaskFailure.from_action_result(result)
                    session.resolve(candidate.id, result, verification)
                    record = ExecutionRecord(
                        task=task,
                        status=TaskStatus.CANCELLED,
                        before=before,
                        after=after,
                        result=result,
                        verification=verification,
                        lifecycle=lifecycle,
                        failure=failure,
                        started_at=started_at,
                    )
                    records.append(record)
                    if self._record_sink is not None:
                        with suppress(Exception):
                            self._record_sink(record)
                    self._append_cancelled(
                        records,
                        tasks[task_index + 1 :],
                        after,
                        "cancelled_after_interruption",
                        self._cancellation.reason,
                    )
                    break
                stage = "post_observation"
                try:
                    after = self._observe()
                except Exception as exc:
                    after = Observation()
                    result = ActionResult.failed(
                        f"Post-action observation failed: {exc}"
                    )
                    failure = TaskFailure.create(
                        ErrorCategory.NAVIGATION,
                        code="post_action_observation_failed",
                        message=result.error,
                    )
                current_observation = after
                lifecycle = lifecycle.transition(TaskPhase.VERIFYING)
                stage = "verification"
                try:
                    verification = self._verifier.verify(
                        task.action,
                        result,
                        before,
                        after,
                        task.expected_result,
                    )
                except Exception as exc:
                    verification = VerificationResult(
                        VerificationStatus.REJECTED,
                        f"Verification failed: {exc}",
                    )
                    failure = classify_exception(
                        exc, stage="verification"
                    )
            except Exception as exc:
                after = Observation()
                label = (
                    "Pre-action observation"
                    if stage == "pre_observation"
                    else "Action execution"
                )
                result = ActionResult.failed(
                    f"{label} failed: {exc}"
                )
                verification = VerificationResult(
                    VerificationStatus.REJECTED,
                    result.error,
                )
                failure = classify_exception(
                    exc,
                    stage=(
                        "observation"
                        if stage == "pre_observation"
                        else "execution"
                    ),
                )
            step = session.resolve(candidate.id, result, verification)
            status = (
                TaskStatus.SUCCEEDED if step is not None else TaskStatus.FAILED
            )
            if status is TaskStatus.SUCCEEDED:
                lifecycle = lifecycle.transition(TaskPhase.SUCCEEDED)
            else:
                lifecycle = lifecycle.transition(TaskPhase.FAILED)
                if failure is None:
                    failure = (
                        TaskFailure.from_action_result(result)
                        if not result.success
                        else TaskFailure.create(
                            ErrorCategory.VERIFICATION,
                            code="verification_rejected",
                            message=verification.reason,
                        )
                    )
            record = ExecutionRecord(
                task=task,
                status=status,
                before=before,
                after=after,
                result=result,
                verification=verification,
                lifecycle=lifecycle,
                failure=failure,
                step_id=step.id if step else "",
                started_at=started_at,
            )
            records.append(record)
            if step is not None:
                steps.append(step)
            if self._record_sink is not None:
                with suppress(Exception):
                    self._record_sink(record)
            if status is TaskStatus.FAILED:
                cancelled_before = current_observation or after
                self._append_cancelled(
                    records,
                    tasks[task_index + 1 :],
                    cancelled_before,
                    "cancelled_after_failure",
                    "Cancelled after a previous task failed",
                )
                break
        if len(records) == len(tasks) and all(
            record.status is TaskStatus.SUCCEEDED for record in records
        ):
            overall = TaskStatus.SUCCEEDED
        elif self._cancellation.cancelled and any(
            record.status is TaskStatus.CANCELLED for record in records
        ):
            overall = TaskStatus.CANCELLED
        else:
            overall = TaskStatus.FAILED
        dump = ExecutionDump(overall, tuple(records), tuple(steps))
        session.record_execution(dump.to_persisted_dict())
        return dump

    def _append_cancelled(
        self,
        records: list[ExecutionRecord],
        tasks: tuple[ExecutionTask, ...],
        observation: Observation,
        code: str,
        message: str,
    ) -> None:
        for task in tasks:
            failure = TaskFailure.create(
                ErrorCategory.CANCELLED, code=code, message=message
            )
            record = ExecutionRecord(
                task=task,
                status=TaskStatus.CANCELLED,
                before=observation,
                after=observation,
                result=ActionResult(
                    ActionStatus.CANCELLED,
                    error=message,
                    error_category=ErrorCategory.CANCELLED.value,
                ),
                verification=VerificationResult(
                    VerificationStatus.INCONCLUSIVE,
                    "The task was not executed",
                ),
                lifecycle=TaskLifecycle.create(task.id).transition(
                    TaskPhase.CANCELLED
                ),
                failure=failure,
            )
            records.append(record)
            if self._record_sink is not None:
                with suppress(Exception):
                    self._record_sink(record)

    def ingest(
        self,
        session: RecordingSession,
        task: ExecutionTask,
        result: ActionResult,
        *,
        before: Observation,
        after: Observation,
        source: StepSource,
    ) -> ExecutionDump:
        """Record an already-performed action through the same audit seam."""
        self._actions.validate(task.action)
        candidate = session.stage(
            task.action,
            intent=task.intent,
            expected_result=task.expected_result,
            source=source,
        )
        try:
            lifecycle = TaskLifecycle.create(task.id).transition(
                TaskPhase.RUNNING
            ).transition(TaskPhase.VERIFYING)
            verification = self._verifier.verify(
                task.action,
                result,
                before,
                after,
                task.expected_result,
            )
        except Exception as exc:
            verification = VerificationResult(
                VerificationStatus.REJECTED,
                f"Verification failed: {exc}",
            )
        failure: TaskFailure | None = None
        step = session.resolve(candidate.id, result, verification)
        if step is not None:
            lifecycle = lifecycle.transition(TaskPhase.SUCCEEDED)
        else:
            lifecycle = lifecycle.transition(TaskPhase.FAILED)
            failure = (
                TaskFailure.from_action_result(result)
                if not result.success
                else TaskFailure.create(
                    ErrorCategory.VERIFICATION,
                    code="verification_rejected",
                    message=verification.reason,
                )
            )
        record = ExecutionRecord(
            task=task,
            status=(
                TaskStatus.SUCCEEDED if step is not None else TaskStatus.FAILED
            ),
            before=before,
            after=after,
            result=result,
            verification=verification,
            lifecycle=lifecycle,
            failure=failure,
            step_id=step.id if step else "",
        )
        if self._record_sink is not None:
            with suppress(Exception):
                self._record_sink(record)
        dump = ExecutionDump(
            record.status,
            (record,),
            (step,) if step is not None else (),
        )
        session.record_execution(dump.to_persisted_dict())
        return dump

    def _observe(self) -> Observation:
        if self._observer is None:
            return Observation()
        return capture_observation(
            self._observer,
            ContextRequest(ContextPurpose.INSIGHT, force_refresh=True),
        )


_SECRET_KEYS = {
    "access_token",
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "refresh_token",
    "secret",
    "set-cookie",
    "token",
    "x-api-key",
}


def _action_timeout_seconds(action: Action) -> float | None:
    return action.timeout_seconds


def _sanitize(value: JsonValue, *, key: str = "") -> JsonValue:
    if key.lower() in _SECRET_KEYS:
        return "[REDACTED]"
    if isinstance(value, str):
        return value if len(value) <= 4_000 else f"{value[:4_000]}…[truncated]"
    if isinstance(value, list):
        return [_sanitize(item) for item in value[:300]]
    if isinstance(value, dict):
        return {
            item_key: _sanitize(item_value, key=item_key)
            for item_key, item_value in list(value.items())[:300]
        }
    return value


__all__ = [
    "ExecutionDump",
    "ExecutionRecord",
    "ExecutionRunner",
    "TaskStatus",
]
