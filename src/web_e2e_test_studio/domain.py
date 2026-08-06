"""Versioned, model-independent intermediate representation for recorded tests."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast
from uuid import uuid4

JsonValue = (
    None
    | bool
    | int
    | float
    | str
    | list["JsonValue"]
    | dict[str, "JsonValue"]
)


def utc_now() -> str:
    """Return an ISO-8601 timestamp suitable for persisted assets."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


class ActionChannel(StrEnum):
    """Execution boundary used by an action."""

    BROWSER = "browser"
    API = "api"
    EXTERNAL = "external"


class StepSource(StrEnum):
    """Where a candidate action came from."""

    MANUAL = "manual"
    VLM = "vlm"
    USER = "user"
    IMPORTED = "imported"


class ActionStatus(StrEnum):
    """Result of deterministic action execution."""

    SUCCESS = "success"
    FAILED = "failed"
    BLOCKED = "blocked"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class VerificationStatus(StrEnum):
    """Independent assessment made after execution and re-observation."""

    VERIFIED = "verified"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


class CandidateStatus(StrEnum):
    """Lifecycle state of a recording candidate."""

    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"
    FAILED = "failed"


@dataclass(frozen=True)
class Locator:
    """A Playwright-oriented locator plus ordered deterministic fallbacks."""

    strategy: str
    value: str
    name: str | None = None
    exact: bool = True
    nth: int | None = None
    fallbacks: tuple[Locator, ...] = ()

    def to_dict(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "strategy": self.strategy,
            "value": self.value,
            "exact": self.exact,
        }
        if self.name is not None:
            data["name"] = self.name
        if self.nth is not None:
            data["nth"] = self.nth
        if self.fallbacks:
            data["fallbacks"] = [item.to_dict() for item in self.fallbacks]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Locator:
        return cls(
            strategy=str(data["strategy"]),
            value=str(data["value"]),
            name=str(data["name"]) if data.get("name") is not None else None,
            exact=bool(data.get("exact", True)),
            nth=int(data["nth"]) if data.get("nth") is not None else None,
            fallbacks=tuple(
                cls.from_dict(item)
                for item in data.get("fallbacks", [])
                if isinstance(item, dict)
            ),
        )


@dataclass(frozen=True)
class FrameRef:
    """One deterministic hop from a document into a child frame."""

    selector: str = ""
    name: str = ""
    url_hint: str = ""
    index: int | None = None

    def to_dict(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {}
        if self.selector:
            data["selector"] = self.selector
        if self.name:
            data["name"] = self.name
        if self.url_hint:
            data["url_hint"] = self.url_hint
        if self.index is not None:
            data["index"] = self.index
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FrameRef:
        return cls(
            selector=str(data.get("selector", "")),
            name=str(data.get("name", "")),
            url_hint=str(data.get("url_hint", "")),
            index=int(data["index"]) if data.get("index") is not None else None,
        )


@dataclass(frozen=True)
class ElementFingerprint:
    """Stable semantic identity used when recorded locators drift."""

    tag: str = ""
    role: str = ""
    accessible_name: str = ""
    text: str = ""
    attributes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "tag": self.tag,
            "role": self.role,
            "accessible_name": self.accessible_name,
            "text": self.text,
            "attributes": dict(self.attributes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ElementFingerprint:
        raw_attributes = data.get("attributes", {})
        return cls(
            tag=str(data.get("tag", "")),
            role=str(data.get("role", "")),
            accessible_name=str(data.get("accessible_name", "")),
            text=str(data.get("text", "")),
            attributes=(
                {str(key): str(value) for key, value in raw_attributes.items()}
                if isinstance(raw_attributes, dict)
                else {}
            ),
        )


@dataclass(frozen=True)
class ElementTarget:
    """Portable identity of an element across tabs, frames, and DOM drift."""

    page_ref: str = "main"
    frame_path: tuple[FrameRef, ...] = ()
    candidates: tuple[Locator, ...] = ()
    fingerprint: ElementFingerprint = field(default_factory=ElementFingerprint)
    element_id: str = ""

    def __post_init__(self) -> None:
        if not self.candidates:
            raise ValueError("ElementTarget requires at least one locator candidate")

    @property
    def locator(self) -> Locator:
        primary, *fallbacks = self.candidates
        return replace(primary, fallbacks=tuple((*primary.fallbacks, *fallbacks)))

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "page_ref": self.page_ref,
            "frame_path": [frame.to_dict() for frame in self.frame_path],
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "fingerprint": self.fingerprint.to_dict(),
            "element_id": self.element_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ElementTarget:
        raw_fingerprint = data.get("fingerprint", {})
        return cls(
            page_ref=str(data.get("page_ref", "main")),
            frame_path=tuple(
                FrameRef.from_dict(item)
                for item in data.get("frame_path", [])
                if isinstance(item, dict)
            ),
            candidates=tuple(
                Locator.from_dict(item)
                for item in data.get("candidates", [])
                if isinstance(item, dict)
            ),
            fingerprint=(
                ElementFingerprint.from_dict(raw_fingerprint)
                if isinstance(raw_fingerprint, dict)
                else ElementFingerprint()
            ),
            element_id=str(data.get("element_id", "")),
        )


@dataclass(frozen=True)
class Action:
    """A validated intent to call one registered deterministic action."""

    name: str
    channel: ActionChannel
    arguments: dict[str, JsonValue] = field(default_factory=dict)
    locator: Locator | None = None
    tool_ref: str | None = None
    timeout_seconds: float | None = None
    target: ElementTarget | None = None

    def __post_init__(self) -> None:
        if self.target is None and self.locator is not None:
            object.__setattr__(self, "target", ElementTarget(candidates=(self.locator,)))
        elif self.target is not None:
            object.__setattr__(self, "locator", self.target.locator)
        if (
            self.timeout_seconds is not None
            and (
                isinstance(self.timeout_seconds, bool)
                or self.timeout_seconds <= 0
            )
        ):
            raise ValueError("Action timeout_seconds must be greater than zero")

    def to_dict(self) -> dict[str, JsonValue]:
        data: dict[str, JsonValue] = {
            "name": self.name,
            "channel": self.channel.value,
            "arguments": self.arguments,
        }
        if self.locator is not None:
            data["locator"] = self.locator.to_dict()
        if self.target is not None:
            data["target"] = self.target.to_dict()
        if self.tool_ref is not None:
            data["tool_ref"] = self.tool_ref
        if self.timeout_seconds is not None:
            data["timeout_seconds"] = self.timeout_seconds
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Action:
        raw_arguments = data.get("arguments", {})
        if not isinstance(raw_arguments, dict):
            raise ValueError("Action arguments must be an object")
        raw_locator = data.get("locator")
        raw_target = data.get("target")
        return cls(
            name=str(data["name"]),
            channel=ActionChannel(str(data["channel"])),
            arguments=dict(raw_arguments),
            locator=(
                Locator.from_dict(raw_locator)
                if isinstance(raw_locator, dict)
                else None
            ),
            target=(
                ElementTarget.from_dict(raw_target)
                if isinstance(raw_target, dict)
                else None
            ),
            tool_ref=(
                str(data["tool_ref"])
                if data.get("tool_ref") is not None
                else None
            ),
            timeout_seconds=(
                float(data["timeout_seconds"])
                if data.get("timeout_seconds") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class ActionResult:
    """Structured result returned by a deterministic action executor."""

    status: ActionStatus
    output: dict[str, JsonValue] = field(default_factory=dict)
    error: str = ""
    error_category: str = ""

    @property
    def success(self) -> bool:
        return self.status is ActionStatus.SUCCESS

    @classmethod
    def succeeded(
        cls, output: dict[str, JsonValue] | None = None
    ) -> ActionResult:
        return cls(status=ActionStatus.SUCCESS, output=output or {})

    @classmethod
    def failed(
        cls, error: str, *, error_category: str = ""
    ) -> ActionResult:
        return cls(
            status=ActionStatus.FAILED,
            error=error,
            error_category=error_category,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "status": self.status.value,
            "output": self.output,
            "error": self.error,
            "error_category": self.error_category,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ActionResult:
        output = data.get("output", {})
        return cls(
            status=ActionStatus(str(data["status"])),
            output=dict(output) if isinstance(output, dict) else {},
            error=str(data.get("error", "")),
            error_category=str(data.get("error_category", "")),
        )


@dataclass(frozen=True)
class VerificationResult:
    """Evidence-backed judgement of whether an action achieved its intent."""

    status: VerificationStatus
    reason: str
    evidence: dict[str, JsonValue] = field(default_factory=dict)

    @property
    def is_verified(self) -> bool:
        return self.status is VerificationStatus.VERIFIED

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "status": self.status.value,
            "reason": self.reason,
            "evidence": self.evidence,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VerificationResult:
        evidence = data.get("evidence", {})
        return cls(
            status=VerificationStatus(str(data["status"])),
            reason=str(data.get("reason", "")),
            evidence=dict(evidence) if isinstance(evidence, dict) else {},
        )


@dataclass(frozen=True)
class Observation:
    """Serializable browser context supplied to planners and verifiers."""

    url: str = ""
    title: str = ""
    screenshot_path: str = ""
    viewport: dict[str, int] = field(default_factory=dict)
    interactive_elements: tuple[dict[str, JsonValue], ...] = ()
    page_text: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "url": self.url,
            "title": self.title,
            "screenshot_path": self.screenshot_path,
            "viewport": dict(self.viewport),
            "interactive_elements": [
                dict(item) for item in self.interactive_elements
            ],
            "page_text": self.page_text,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Observation:
        viewport = data.get("viewport", {})
        return cls(
            url=str(data.get("url", "")),
            title=str(data.get("title", "")),
            screenshot_path=str(data.get("screenshot_path", "")),
            viewport={
                str(key): int(value)
                for key, value in (
                    viewport.items() if isinstance(viewport, dict) else []
                )
            },
            interactive_elements=tuple(
                dict(item)
                for item in data.get("interactive_elements", [])
                if isinstance(item, dict)
            ),
            page_text=str(data.get("page_text", "")),
        )


@dataclass(frozen=True)
class StepCandidate:
    """Uncommitted action proposed by a user, recorder, or VLM."""

    id: str
    action: Action
    intent: str
    expected_result: str
    source: StepSource
    created_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "action": self.action.to_dict(),
            "intent": self.intent,
            "expected_result": self.expected_result,
            "source": self.source.value,
            "created_at": self.created_at,
        }


@dataclass(frozen=True)
class RecordedStep:
    """A verified, replayable action eligible for deterministic export."""

    id: str
    action: Action
    intent: str
    expected_result: str
    source: StepSource
    verification: VerificationResult
    recorded_at: str = field(default_factory=utc_now)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "id": self.id,
            "action": self.action.to_dict(),
            "intent": self.intent,
            "expected_result": self.expected_result,
            "source": self.source.value,
            "verification": self.verification.to_dict(),
            "recorded_at": self.recorded_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RecordedStep:
        return cls(
            id=str(data["id"]),
            action=Action.from_dict(_require_mapping(data, "action")),
            intent=str(data.get("intent", "")),
            expected_result=str(data.get("expected_result", "")),
            source=StepSource(str(data["source"])),
            verification=VerificationResult.from_dict(
                _require_mapping(data, "verification")
            ),
            recorded_at=str(data.get("recorded_at", "")) or utc_now(),
        )


@dataclass(frozen=True)
class AttemptRecord:
    """Audit record for a candidate, including actions excluded from export."""

    candidate: StepCandidate
    result: ActionResult
    verification: VerificationResult
    status: CandidateStatus
    resolved_at: str = field(default_factory=utc_now)


@dataclass
class TestAsset:
    """Versioned source of truth consumed by replay and exporters."""

    id: str
    name: str
    goal: str
    base_url: str = ""
    steps: list[RecordedStep] = field(default_factory=list)
    tool_dependencies: dict[str, dict[str, JsonValue]] = field(
        default_factory=dict
    )
    metadata: dict[str, JsonValue] = field(default_factory=dict)
    execution_history: list[dict[str, JsonValue]] = field(
        default_factory=list
    )
    created_at: str = field(default_factory=utc_now)
    schema: str = "web_e2e_test_asset.v1"

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": self.schema,
            "id": self.id,
            "name": self.name,
            "goal": self.goal,
            "base_url": self.base_url,
            "created_at": self.created_at,
            "steps": [step.to_dict() for step in self.steps],
            "tool_dependencies": cast(JsonValue, self.tool_dependencies),
            "metadata": self.metadata,
            "execution_history": cast(JsonValue, self.execution_history),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TestAsset:
        if data.get("schema") != "web_e2e_test_asset.v1":
            raise ValueError("Unsupported test asset schema")
        dependencies = data.get("tool_dependencies", {})
        metadata = data.get("metadata", {})
        execution_history = data.get("execution_history", [])
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            goal=str(data.get("goal", "")),
            base_url=str(data.get("base_url", "")),
            steps=[
                RecordedStep.from_dict(item)
                for item in data.get("steps", [])
                if isinstance(item, dict)
            ],
            tool_dependencies=(
                {
                    str(key): dict(value)
                    for key, value in dependencies.items()
                    if isinstance(value, dict)
                }
                if isinstance(dependencies, dict)
                else {}
            ),
            metadata=dict(metadata) if isinstance(metadata, dict) else {},
            execution_history=[
                dict(item)
                for item in execution_history
                if isinstance(item, dict)
            ]
            if isinstance(execution_history, list)
            else [],
            created_at=str(data.get("created_at", "")) or utc_now(),
        )

    @classmethod
    def create(cls, name: str, goal: str, base_url: str = "") -> TestAsset:
        return cls(
            id=f"asset_{uuid4().hex[:12]}",
            name=name,
            goal=goal,
            base_url=base_url,
        )


def _require_mapping(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"{key} must be an object")
    return value


__all__ = [
    "Action",
    "ActionChannel",
    "ActionResult",
    "ActionStatus",
    "AttemptRecord",
    "CandidateStatus",
    "ElementFingerprint",
    "ElementTarget",
    "FrameRef",
    "JsonValue",
    "Locator",
    "Observation",
    "RecordedStep",
    "StepCandidate",
    "StepSource",
    "TestAsset",
    "VerificationResult",
    "VerificationStatus",
]
