"""Canonical task lifecycle and failure taxonomy."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from web_e2e_test_studio.domain import (
    ActionResult,
    ActionStatus,
    JsonValue,
    utc_now,
)


class TaskPhase(StrEnum):
    """State of one deterministic task from compilation to termination."""

    QUEUED = "queued"
    RUNNING = "running"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            TaskPhase.SUCCEEDED,
            TaskPhase.FAILED,
            TaskPhase.CANCELLED,
        }


_ALLOWED_TRANSITIONS: dict[TaskPhase, frozenset[TaskPhase]] = {
    TaskPhase.QUEUED: frozenset(
        {TaskPhase.RUNNING, TaskPhase.CANCELLED}
    ),
    TaskPhase.RUNNING: frozenset(
        {TaskPhase.VERIFYING, TaskPhase.FAILED, TaskPhase.CANCELLED}
    ),
    TaskPhase.VERIFYING: frozenset(
        {TaskPhase.SUCCEEDED, TaskPhase.FAILED, TaskPhase.CANCELLED}
    ),
    TaskPhase.SUCCEEDED: frozenset(),
    TaskPhase.FAILED: frozenset(),
    TaskPhase.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class LifecycleTransition:
    phase: TaskPhase
    at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, JsonValue]:
        return {"phase": self.phase.value, "at": self.at}


@dataclass(frozen=True)
class TaskLifecycle:
    task_id: str
    phase: TaskPhase = TaskPhase.QUEUED
    transitions: tuple[LifecycleTransition, ...] = ()

    def __post_init__(self) -> None:
        if not self.task_id.strip():
            raise ValueError("Task lifecycle requires a task id")
        if not self.transitions:
            if self.phase is not TaskPhase.QUEUED:
                raise ValueError("Task lifecycle must begin in queued")
            object.__setattr__(
                self,
                "transitions",
                (LifecycleTransition(self.phase),),
            )
            return
        if self.transitions[0].phase is not TaskPhase.QUEUED:
            raise ValueError("Task lifecycle must begin in queued")
        for previous, current in zip(
            self.transitions, self.transitions[1:], strict=False
        ):
            if current.phase not in _ALLOWED_TRANSITIONS[previous.phase]:
                raise ValueError(
                    "Invalid task transition: "
                    f"{previous.phase.value} -> {current.phase.value}"
                )
        if self.transitions[-1].phase is not self.phase:
            raise ValueError(
                "Task lifecycle phase must match its final transition"
            )

    @classmethod
    def create(cls, task_id: str) -> TaskLifecycle:
        return cls(task_id=task_id)

    def transition(self, phase: TaskPhase) -> TaskLifecycle:
        if self.phase.terminal:
            raise ValueError(
                f"Task {self.task_id} is terminal in {self.phase.value}"
            )
        if phase not in _ALLOWED_TRANSITIONS[self.phase]:
            raise ValueError(
                f"Invalid task transition: {self.phase.value} -> {phase.value}"
            )
        return replace(
            self,
            phase=phase,
            transitions=(*self.transitions, LifecycleTransition(phase)),
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "task_id": self.task_id,
            "phase": self.phase.value,
            "transitions": [item.to_dict() for item in self.transitions],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskLifecycle:
        raw_transitions = data.get("transitions", [])
        if not isinstance(raw_transitions, list):
            raise ValueError("Task lifecycle transitions must be a list")
        return cls(
            task_id=str(data["task_id"]),
            phase=TaskPhase(str(data["phase"])),
            transitions=tuple(
                LifecycleTransition(
                    TaskPhase(str(item["phase"])),
                    str(item.get("at", "")) or utc_now(),
                )
                for item in raw_transitions
                if isinstance(item, dict)
            ),
        )


class ErrorCategory(StrEnum):
    """Stable failure family used by retry, recovery, UI, and reports."""

    PROTOCOL = "protocol"
    LOCATE = "locate"
    AMBIGUOUS = "ambiguous"
    NAVIGATION = "navigation"
    EXECUTION = "execution"
    VERIFICATION = "verification"
    CAPABILITY = "capability"
    INFRASTRUCTURE = "infrastructure"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RecoveryDirective:
    """Machine-readable response to a classified task failure."""

    refresh_context: bool = False
    replan: bool = False
    ask_user: bool = False

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "refresh_context": self.refresh_context,
            "replan": self.replan,
            "ask_user": self.ask_user,
        }


@dataclass(frozen=True)
class FailurePolicy:
    retryable: bool
    recovery: RecoveryDirective


_RECOVERY_POLICIES: dict[ErrorCategory, FailurePolicy] = {
    ErrorCategory.PROTOCOL: FailurePolicy(
        True, RecoveryDirective(replan=True)
    ),
    ErrorCategory.LOCATE: (
        FailurePolicy(
            True,
            RecoveryDirective(refresh_context=True, replan=True),
        )
    ),
    ErrorCategory.AMBIGUOUS: (
        FailurePolicy(
            True,
            RecoveryDirective(refresh_context=True, replan=True),
        )
    ),
    ErrorCategory.NAVIGATION: (
        FailurePolicy(
            True,
            RecoveryDirective(refresh_context=True, replan=True),
        )
    ),
    ErrorCategory.EXECUTION: FailurePolicy(
        False, RecoveryDirective(replan=True)
    ),
    ErrorCategory.VERIFICATION: (
        FailurePolicy(
            True,
            RecoveryDirective(refresh_context=True, replan=True),
        )
    ),
    ErrorCategory.CAPABILITY: (
        FailurePolicy(False, RecoveryDirective(ask_user=True))
    ),
    ErrorCategory.INFRASTRUCTURE: FailurePolicy(
        True, RecoveryDirective()
    ),
    ErrorCategory.TIMEOUT: FailurePolicy(
        True, RecoveryDirective(replan=True)
    ),
    ErrorCategory.CANCELLED: FailurePolicy(
        False, RecoveryDirective()
    ),
}


@dataclass(frozen=True)
class TaskFailure:
    """Serializable failure with a stable category and recovery policy."""

    category: ErrorCategory
    code: str
    message: str
    retryable: bool
    recovery: RecoveryDirective

    @classmethod
    def create(
        cls,
        category: ErrorCategory,
        *,
        code: str,
        message: str,
    ) -> TaskFailure:
        policy = _RECOVERY_POLICIES[category]
        return cls(
            category, code, message, policy.retryable, policy.recovery
        )

    @classmethod
    def from_action_result(cls, result: ActionResult) -> TaskFailure:
        try:
            persisted_category = ErrorCategory(result.error_category)
        except ValueError:
            persisted_category = None
        if persisted_category is not None:
            category = persisted_category
        elif result.status is ActionStatus.TIMEOUT:
            category = ErrorCategory.TIMEOUT
        elif result.status is ActionStatus.BLOCKED:
            category = ErrorCategory.CAPABILITY
        else:
            category = ErrorCategory.EXECUTION
        return cls.create(
            category,
            code=f"action_{result.status.value}",
            message=result.error or f"Action ended with {result.status.value}",
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "category": self.category.value,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "recovery": self.recovery.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskFailure:
        category = ErrorCategory(str(data["category"]))
        # Recovery policy is owned by the category, not persisted input.
        return cls.create(
            category,
            code=str(data["code"]),
            message=str(data.get("message", "")),
        )


def classify_exception(
    error: Exception,
    *,
    stage: str = "infrastructure",
) -> TaskFailure:
    """Classify an exception at a known orchestration boundary."""
    name = type(error).__name__
    folded_name = name.casefold()
    if "cancelled" in folded_name or "canceled" in folded_name:
        category = ErrorCategory.CANCELLED
    elif isinstance(error, TimeoutError) or "timeout" in folded_name:
        category = ErrorCategory.TIMEOUT
    elif "locatorresolution" in folded_name:
        candidates = getattr(error, "candidates", ())
        category = (
            ErrorCategory.AMBIGUOUS
            if any(getattr(item, "count", 0) > 1 for item in candidates)
            else ErrorCategory.LOCATE
        )
    elif name == "PlannerProtocolError" or stage == "protocol":
        category = ErrorCategory.PROTOCOL
    elif stage == "observation":
        category = ErrorCategory.NAVIGATION
    elif stage == "verification":
        category = ErrorCategory.VERIFICATION
    elif (
        "capability" in folded_name
        or "notregistered" in folded_name
        or stage == "capability"
    ):
        category = ErrorCategory.CAPABILITY
    elif stage == "execution":
        category = ErrorCategory.EXECUTION
    else:
        category = ErrorCategory.INFRASTRUCTURE
    return TaskFailure.create(
        category,
        code=f"{stage}_{name}",
        message=str(error) or name,
    )


@dataclass(frozen=True)
class TaskStateSnapshot:
    """Validated lifecycle and optional failure persisted by Desktop."""

    lifecycle: TaskLifecycle
    failure: TaskFailure | None = None

    def __post_init__(self) -> None:
        failed = self.lifecycle.phase in {
            TaskPhase.FAILED,
            TaskPhase.CANCELLED,
        }
        if failed is not (self.failure is not None):
            raise ValueError(
                "Failed or cancelled task state must contain a failure"
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "lifecycle": self.lifecycle.to_dict(),
            "failure": self.failure.to_dict() if self.failure else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskStateSnapshot:
        raw_lifecycle = data.get("lifecycle")
        if not isinstance(raw_lifecycle, dict):
            raise ValueError("Task state lifecycle must be an object")
        raw_failure = data.get("failure")
        return cls(
            TaskLifecycle.from_dict(raw_lifecycle),
            TaskFailure.from_dict(raw_failure)
            if isinstance(raw_failure, dict)
            else None,
        )


__all__ = [
    "ErrorCategory",
    "LifecycleTransition",
    "RecoveryDirective",
    "TaskFailure",
    "TaskLifecycle",
    "TaskPhase",
    "TaskStateSnapshot",
    "classify_exception",
]
