"""Plan-compile-execute-replan authoring workflow."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from web_e2e_test_studio.agent.actions import ActionRegistry, ActionSpec
from web_e2e_test_studio.agent.control import CancellationToken
from web_e2e_test_studio.agent.execution import (
    ExecutionDump,
    ExecutionRecord,
    ExecutionRunner,
    Verifier,
)
from web_e2e_test_studio.agent.lifecycle import (
    ErrorCategory,
    TaskFailure,
    classify_exception,
)
from web_e2e_test_studio.agent.planning import (
    PlanCompiler,
    PlanningResult,
    PlanningStatus,
)
from web_e2e_test_studio.context import (
    ContextPurpose,
    ContextRequest,
    capture_observation,
)
from web_e2e_test_studio.domain import (
    JsonValue,
    Observation,
    RecordedStep,
    utc_now,
)
from web_e2e_test_studio.recording import RecordingSession


class RunStatus(StrEnum):
    """Terminal state of one authoring request."""

    COMPLETED = "completed"
    WAITING_FOR_USER = "waiting_for_user"
    EXHAUSTED = "exhausted"
    INCONCLUSIVE = "inconclusive"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class AgentFeedback:
    """One closed planning cycle supplied to subsequent planner calls."""

    plan: PlanningResult
    execution: ExecutionDump


@dataclass(frozen=True)
class AgentEvent:
    """Public structured progress event for CLI, API, and Studio."""

    kind: str
    payload: dict[str, JsonValue] = field(default_factory=dict)
    at: str = field(default_factory=utc_now)


@dataclass(frozen=True)
class AgentRunResult:
    """Terminal authoring outcome and its auditable event stream."""

    status: RunStatus
    events: tuple[AgentEvent, ...]
    steps: tuple[RecordedStep, ...] = ()
    executions: tuple[ExecutionDump, ...] = ()
    summary: str = ""
    question: str = ""


class Observer(Protocol):
    def observe(self) -> Observation: ...


class Planner(Protocol):
    def plan(
        self,
        request: str,
        observation: Observation,
        history: Sequence[AgentFeedback],
        actions: Sequence[ActionSpec],
    ) -> PlanningResult: ...


class AuthoringAgent:
    """Keep model planning separate from deterministic execution."""

    def __init__(
        self,
        *,
        observer: Observer,
        planner: Planner,
        verifier: Verifier,
        actions: ActionRegistry,
        max_rounds: int = 12,
        event_sink: Callable[[AgentEvent], None] | None = None,
        cancellation: CancellationToken | None = None,
        inputs: dict[str, JsonValue] | None = None,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        self._observer = observer
        self._planner = planner
        self._verifier = verifier
        self._actions = actions
        self._max_rounds = max_rounds
        self._event_sink = event_sink
        self._compiler = PlanCompiler(actions)
        self._cancellation = cancellation or CancellationToken()
        self._inputs = inputs or {}

    def perform(
        self,
        session: RecordingSession,
        instruction: str,
        *,
        initial_memories: Sequence[str] = (),
    ) -> AgentRunResult:
        events: list[AgentEvent] = []
        history: list[AgentFeedback] = []
        executions: list[ExecutionDump] = []
        committed: list[RecordedStep] = []
        memories = list(dict.fromkeys(
            memory.strip() for memory in initial_memories if memory.strip()
        ))

        def emit(kind: str, payload: dict[str, JsonValue]) -> None:
            event = AgentEvent(kind, payload)
            events.append(event)
            if self._event_sink is not None:
                with suppress(Exception):
                    self._event_sink(event)

        def record_execution(record: ExecutionRecord) -> None:
            if record.lifecycle is None:
                raise RuntimeError("Execution record has no task lifecycle")
            emit(
                "task_finished",
                {
                    "task_id": record.task.id,
                    "action": record.task.action.name,
                    "channel": record.task.action.channel.value,
                    "status": record.status.value,
                    "execution_status": record.result.status.value,
                    "verification": record.verification.status.value,
                    "reason": record.verification.reason,
                    "step_id": record.step_id,
                    "lifecycle": record.lifecycle.to_dict(),
                    "failure": (
                        record.failure.to_dict()
                        if record.failure is not None
                        else None
                    ),
                },
            )
            if record.step_id:
                emit(
                    "step_committed",
                    {
                        "step_id": record.step_id,
                        "action": record.task.action.name,
                    },
                )

        runner = ExecutionRunner(
            observer=self._observer,
            verifier=self._verifier,
            actions=self._actions,
            record_sink=record_execution,
            cancellation=self._cancellation,
            inputs=self._inputs,
        )

        for round_number in range(1, self._max_rounds + 1):
            try:
                observation = capture_observation(
                    self._observer,
                    ContextRequest(
                        ContextPurpose.PLANNING,
                        force_refresh=round_number == 1,
                    ),
                )
            except Exception as exc:
                failure = classify_exception(exc, stage="observation")
                emit("run_failed", {"failure": failure.to_dict()})
                raise
            emit(
                "observation",
                {
                    "round": round_number,
                    "url": observation.url,
                    "title": observation.title,
                },
            )
            set_memories = getattr(self._planner, "set_memories", None)
            if callable(set_memories):
                set_memories(tuple(memories))
            try:
                plan = self._planner.plan(
                    instruction,
                    observation,
                    tuple(history),
                    self._actions.available_for_planner(),
                )
            except Exception as exc:
                failure = classify_exception(exc, stage="planning")
                emit("run_failed", {"failure": failure.to_dict()})
                raise
            emit(
                "plan",
                {
                    "round": round_number,
                    "status": plan.status.value,
                    "thought": plan.thought,
                    "confidence": plan.confidence,
                    "action_count": len(plan.planned_actions),
                },
            )
            if plan.memory and plan.memory not in memories:
                memories.append(plan.memory)
                emit(
                    "memory",
                    {
                        "round": round_number,
                        "memory": plan.memory,
                        "memory_count": len(memories),
                    },
                )

            if plan.status is PlanningStatus.ASK:
                emit("question", {"question": plan.question})
                return AgentRunResult(
                    RunStatus.WAITING_FOR_USER,
                    tuple(events),
                    tuple(committed),
                    tuple(executions),
                    question=plan.question,
                )

            if plan.status is PlanningStatus.COMPLETE:
                status = (
                    RunStatus.COMPLETED
                    if committed or session.asset.steps
                    else RunStatus.INCONCLUSIVE
                )
                emit(
                    "done",
                    {
                        "summary": plan.summary,
                        "verified_steps": len(committed),
                        "status": status.value,
                    },
                )
                return AgentRunResult(
                    status,
                    tuple(events),
                    tuple(committed),
                    tuple(executions),
                    summary=plan.summary,
                )

            try:
                tasks = self._compiler.compile(plan)
            except ValueError as exc:
                failure = TaskFailure.create(
                    ErrorCategory.PROTOCOL,
                    code="plan_compilation_rejected",
                    message=str(exc),
                )
                emit(
                    "plan_rejected",
                    {
                        "round": round_number,
                        "error": str(exc),
                        "action_count": len(plan.planned_actions),
                        "failure": failure.to_dict(),
                    },
                )
                return AgentRunResult(
                    RunStatus.INCONCLUSIVE,
                    tuple(events),
                    tuple(committed),
                    tuple(executions),
                    summary=f"Plan rejected before execution: {exc}",
                )
            execution = runner.run(
                session,
                tasks,
                initial_observation=observation,
            )
            executions.append(execution)
            committed.extend(execution.steps)
            if self._cancellation.cancelled:
                emit(
                    "cancelled",
                    {
                        "round": round_number,
                        "reason": self._cancellation.reason,
                        "verified_steps": len(committed),
                    },
                )
                return AgentRunResult(
                    RunStatus.CANCELLED,
                    tuple(events),
                    tuple(committed),
                    tuple(executions),
                    summary=self._cancellation.reason,
                )
            history.append(AgentFeedback(plan, execution))

        emit(
            "exhausted",
            {
                "rounds": self._max_rounds,
                "verified_steps": len(committed),
            },
        )
        return AgentRunResult(
            RunStatus.EXHAUSTED,
            tuple(events),
            tuple(committed),
            tuple(executions),
            summary="The authoring round limit was reached",
        )


__all__ = [
    "AgentEvent",
    "AgentFeedback",
    "AgentRunResult",
    "AuthoringAgent",
    "Planner",
    "RunStatus",
]
