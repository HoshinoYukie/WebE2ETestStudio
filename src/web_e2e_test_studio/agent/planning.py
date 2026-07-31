"""Strict model planning contract and deterministic plan compiler."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4

from web_e2e_test_studio.agent.actions import ActionRegistry
from web_e2e_test_studio.domain import Action


class PlanningStatus(StrEnum):
    """The three mutually exclusive outcomes of one planning cycle."""

    ACTIONS = "actions"
    ASK = "ask"
    COMPLETE = "complete"


@dataclass(frozen=True)
class PlannedAction:
    """One model-proposed action before deterministic compilation."""

    action: Action
    intent: str
    expected_result: str


@dataclass(frozen=True)
class PlanningResult:
    """Validated planner output; terminal states cannot contain actions."""

    status: PlanningStatus
    planned_actions: tuple[PlannedAction, ...] = ()
    thought: str = ""
    confidence: float = 0.0
    memory: str = ""
    question: str = ""
    summary: str = ""

    def __post_init__(self) -> None:
        if self.status is PlanningStatus.ACTIONS and not self.planned_actions:
            raise ValueError("An actions plan requires at least one action")
        if self.status is not PlanningStatus.ACTIONS and self.planned_actions:
            raise ValueError("A terminal plan cannot contain actions")
        if self.status is PlanningStatus.ASK and not self.question.strip():
            raise ValueError("An ask plan requires a question")
        if self.status is not PlanningStatus.ASK and self.question:
            raise ValueError("Only an ask plan can contain a question")
        if self.status is not PlanningStatus.COMPLETE and self.summary:
            raise ValueError("Only a complete plan can contain a summary")
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError("Plan confidence must be between 0 and 1")

    @classmethod
    def actions(
        cls,
        actions: tuple[PlannedAction, ...],
        *,
        thought: str = "",
        confidence: float = 0.0,
        memory: str = "",
    ) -> PlanningResult:
        return cls(
            PlanningStatus.ACTIONS,
            actions,
            thought=thought,
            confidence=confidence,
            memory=memory,
        )

    @classmethod
    def ask(cls, question: str, *, memory: str = "") -> PlanningResult:
        return cls(PlanningStatus.ASK, memory=memory, question=question)

    @classmethod
    def complete(
        cls, summary: str = "", *, memory: str = ""
    ) -> PlanningResult:
        return cls(PlanningStatus.COMPLETE, memory=memory, summary=summary)


@dataclass(frozen=True)
class ExecutionTask:
    """A validated deterministic task produced by the compiler."""

    id: str
    action: Action
    intent: str
    expected_result: str


class PlanCompiler:
    """Compile and validate an entire plan before any side effect occurs."""

    def __init__(self, actions: ActionRegistry) -> None:
        self._actions = actions

    def compile(self, plan: PlanningResult) -> tuple[ExecutionTask, ...]:
        if plan.status is not PlanningStatus.ACTIONS:
            raise ValueError("Only an actions plan can be compiled")
        for proposed in plan.planned_actions:
            self._actions.validate(proposed.action)
        return tuple(
            ExecutionTask(
                id=f"task_{uuid4().hex[:12]}",
                action=proposed.action,
                intent=proposed.intent,
                expected_result=proposed.expected_result,
            )
            for proposed in plan.planned_actions
        )


__all__ = [
    "ExecutionTask",
    "PlanCompiler",
    "PlannedAction",
    "PlanningResult",
    "PlanningStatus",
]
