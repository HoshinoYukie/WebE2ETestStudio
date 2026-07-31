"""Action schemas shared by planners, executors, recording, and export."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Protocol

from web_e2e_test_studio.agent.control import ActionExecutionContext
from web_e2e_test_studio.agent.lifecycle import (
    ErrorCategory,
    classify_exception,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    ActionStatus,
    JsonValue,
)


class ActionExecutor(Protocol):
    """Deterministic boundary capable of executing a structured action."""

    def execute(self, action: Action) -> ActionResult: ...


DEFAULT_WAIT_MILLISECONDS = 1_000


def resolve_wait_milliseconds(action: Action) -> int | float:
    """Normalize every supported wait duration spelling."""
    value: JsonValue = DEFAULT_WAIT_MILLISECONDS
    for key in ("milliseconds", "timeout", "timeout_ms", "time_ms"):
        if key in action.arguments:
            value = action.arguments[key]
            break
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or value < 0
    ):
        raise ValueError(
            "wait duration must be a non-negative number "
            "(milliseconds, timeout, timeout_ms, or time_ms)"
        )
    return value


@dataclass(frozen=True)
class ActionSpec:
    """Model-facing action description with lightweight runtime validation."""

    name: str
    channel: ActionChannel
    description: str
    locator_required: bool = False
    required_arguments: tuple[str, ...] = ()
    argument_schema: dict[str, JsonValue] = field(default_factory=dict)
    user_initiated_only: bool = False

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "channel": self.channel.value,
            "description": self.description,
            "locator_required": self.locator_required,
            "required_arguments": list(self.required_arguments),
            "argument_schema": self.argument_schema,
            "user_initiated_only": self.user_initiated_only,
        }

    def validate(self, action: Action) -> None:
        if action.name != self.name:
            raise ValueError(
                f"Action name {action.name!r} does not match {self.name!r}"
            )
        if action.channel is not self.channel:
            raise ValueError(
                f"Action {action.name!r} must use channel {self.channel.value!r}"
            )
        if self.locator_required and action.locator is None:
            raise ValueError(f"Action {action.name!r} requires a locator")
        missing = [
            key for key in self.required_arguments if key not in action.arguments
        ]
        if missing:
            raise ValueError(
                f"Action {action.name!r} is missing arguments: {', '.join(missing)}"
            )
        if self.argument_schema:
            unknown = set(action.arguments) - set(self.argument_schema)
            if unknown:
                raise ValueError(
                    f"Action {action.name!r} has unknown arguments: "
                    + ", ".join(sorted(unknown))
                )
            for key, value in action.arguments.items():
                expected = self.argument_schema.get(key)
                if isinstance(expected, str) and not _matches_type(
                    value, expected
                ):
                    raise ValueError(
                        f"Action {action.name!r} argument {key!r} "
                        f"must be {expected}"
                    )


def _matches_type(value: JsonValue, expected: str) -> bool:
    if expected == "json":
        return True
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    raise ValueError(f"Unsupported action argument type: {expected}")


class ActionRegistry:
    """Registry that keeps model schemas and deterministic executors aligned."""

    def __init__(self) -> None:
        self._entries: dict[
            tuple[ActionChannel, str], tuple[ActionSpec, ActionExecutor]
        ] = {}

    def register(self, spec: ActionSpec, executor: ActionExecutor) -> None:
        key = (spec.channel, spec.name)
        if key in self._entries:
            raise ValueError(
                f"Action is already registered: {spec.channel.value}/{spec.name}"
            )
        self._entries[key] = (spec, executor)

    def validate(self, action: Action) -> ActionSpec:
        entry = self._entries.get((action.channel, action.name))
        if entry is None:
            raise ValueError(
                f"Unknown action: {action.channel.value}/{action.name}"
            )
        spec, _executor = entry
        spec.validate(action)
        return spec

    def execute(
        self,
        action: Action,
        control: ActionExecutionContext | None = None,
        *,
        allow_user_initiated: bool = False,
    ) -> ActionResult:
        entry = self._entries.get((action.channel, action.name))
        if entry is None:
            return ActionResult.failed(
                f"Unknown action: {action.channel.value}/{action.name}"
            )
        spec, executor = entry
        try:
            spec.validate(action)
            if spec.user_initiated_only and not allow_user_initiated:
                return ActionResult(
                    ActionStatus.BLOCKED,
                    error=(
                        f"Action {action.name} requires an explicit "
                        "user-initiated command"
                    ),
                    error_category="authorization",
                )
            if control is not None and control.cancelled:
                return control.cancelled_result()
            parameters = inspect.signature(executor.execute).parameters
            if control is not None and len(parameters) >= 2:
                result = executor.execute(action, control)  # type: ignore[call-arg]
            else:
                result = executor.execute(action)
            if control is not None:
                if control.cancelled:
                    return control.cancelled_result()
                if control.timed_out:
                    return control.timeout_result()
            return result
        except Exception as exc:
            failure = classify_exception(exc, stage="execution")
            status = (
                ActionStatus.CANCELLED
                if failure.category is ErrorCategory.CANCELLED
                else (
                    ActionStatus.TIMEOUT
                    if failure.category is ErrorCategory.TIMEOUT
                    else ActionStatus.FAILED
                )
            )
            return ActionResult(
                status,
                error=str(exc),
                error_category=failure.category.value,
            )

    def available(self) -> tuple[ActionSpec, ...]:
        return tuple(spec for spec, _executor in self._entries.values())

    def available_for_planner(self) -> tuple[ActionSpec, ...]:
        """Expose only actions the model is authorized to select."""
        return tuple(
            spec
            for spec, _executor in self._entries.values()
            if not spec.user_initiated_only
        )
