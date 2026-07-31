"""Candidate staging and the verification gate for recorded assets."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from uuid import uuid4

from web_e2e_test_studio.domain import (
    Action,
    ActionResult,
    AttemptRecord,
    CandidateStatus,
    JsonValue,
    Locator,
    RecordedStep,
    StepCandidate,
    StepSource,
    TestAsset,
    VerificationResult,
    VerificationStatus,
)


@dataclass
class RecordingSession:
    """Owns one asset and prevents unverified actions from entering it."""

    asset: TestAsset
    attempts: list[AttemptRecord] = field(default_factory=list)
    _pending: dict[str, StepCandidate] = field(default_factory=dict)

    @classmethod
    def create(
        cls, *, name: str, goal: str, base_url: str = ""
    ) -> RecordingSession:
        return cls(asset=TestAsset.create(name, goal, base_url))

    def stage(
        self,
        action: Action,
        *,
        intent: str,
        expected_result: str,
        source: StepSource,
    ) -> StepCandidate:
        candidate = StepCandidate(
            id=f"candidate_{uuid4().hex[:12]}",
            action=action,
            intent=intent,
            expected_result=expected_result,
            source=source,
        )
        self._pending[candidate.id] = candidate
        return candidate

    def resolve(
        self,
        candidate_id: str,
        result: ActionResult,
        verification: VerificationResult,
    ) -> RecordedStep | None:
        try:
            candidate = self._pending.pop(candidate_id)
        except KeyError as exc:
            raise KeyError(f"Unknown or already resolved candidate: {candidate_id}") from exc

        status = _candidate_status(result, verification)
        self.attempts.append(
            AttemptRecord(
                candidate=candidate,
                result=result,
                verification=verification,
                status=status,
            )
        )
        if status is not CandidateStatus.VERIFIED:
            return None

        step = RecordedStep(
            id=f"step_{uuid4().hex[:12]}",
            action=_resolved_action(candidate.action, result),
            intent=candidate.intent,
            expected_result=candidate.expected_result,
            source=candidate.source,
            verification=verification,
        )
        self.asset.steps.append(step)
        return step

    def record_execution(self, execution: dict[str, JsonValue]) -> None:
        """Persist one structured execution dump in the model-independent asset."""
        self.asset.execution_history.append(execution)

    @property
    def pending(self) -> tuple[StepCandidate, ...]:
        return tuple(self._pending.values())


def _candidate_status(
    result: ActionResult, verification: VerificationResult
) -> CandidateStatus:
    if not result.success:
        if verification.status is VerificationStatus.REJECTED:
            return CandidateStatus.REJECTED
        return CandidateStatus.FAILED
    if verification.status is VerificationStatus.VERIFIED:
        return CandidateStatus.VERIFIED
    if verification.status is VerificationStatus.REJECTED:
        return CandidateStatus.REJECTED
    return CandidateStatus.INCONCLUSIVE


def _resolved_action(action: Action, result: ActionResult) -> Action:
    raw_resolution = result.output.get("locator_resolution")
    if action.locator is None or not isinstance(raw_resolution, dict):
        return action
    raw_selected = raw_resolution.get("selected")
    if not isinstance(raw_selected, dict):
        return action
    try:
        selected = Locator.from_dict(raw_selected)
    except (KeyError, TypeError, ValueError):
        return action
    if selected == action.locator:
        return action
    selected = replace(selected, fallbacks=(action.locator,))
    return replace(action, locator=selected)
