"""Structured authoring agent and deterministic action registry."""

from .actions import ActionRegistry, ActionSpec
from .execution import (
    ExecutionDump,
    ExecutionRecord,
    ExecutionRunner,
    TaskStatus,
)
from .lifecycle import (
    ErrorCategory,
    LifecycleTransition,
    RecoveryDirective,
    TaskFailure,
    TaskLifecycle,
    TaskPhase,
    TaskStateSnapshot,
    classify_exception,
)
from .loop import (
    AgentEvent,
    AgentRunResult,
    AuthoringAgent,
    RunStatus,
)
from .planning import (
    ExecutionTask,
    PlanCompiler,
    PlannedAction,
    PlanningResult,
    PlanningStatus,
)

__all__ = [
    "ActionRegistry",
    "ActionSpec",
    "AgentEvent",
    "AgentRunResult",
    "AuthoringAgent",
    "ExecutionDump",
    "ExecutionRecord",
    "ExecutionRunner",
    "ExecutionTask",
    "ErrorCategory",
    "LifecycleTransition",
    "PlanCompiler",
    "PlannedAction",
    "PlanningResult",
    "PlanningStatus",
    "RunStatus",
    "RecoveryDirective",
    "TaskFailure",
    "TaskLifecycle",
    "TaskPhase",
    "TaskStateSnapshot",
    "classify_exception",
    "TaskStatus",
]
