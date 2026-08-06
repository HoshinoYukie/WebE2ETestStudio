"""Ranked, deterministic locator selection independent from Playwright actions."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from web_e2e_test_studio.domain import JsonValue, Locator, Observation


@dataclass(frozen=True)
class LocatorProbe:
    """Live facts collected without performing an action."""

    count: int
    visible: bool
    enabled: bool = True
    actionable: bool = True


@dataclass(frozen=True)
class LocatorCandidate:
    """One ranked locator and the evidence used to accept or reject it."""

    locator: Locator
    source: str
    score: int
    count: int
    visible: bool
    enabled: bool
    actionable: bool
    viable: bool
    reason: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "locator": self.locator.to_dict(),
            "source": self.source,
            "score": self.score,
            "count": self.count,
            "visible": self.visible,
            "enabled": self.enabled,
            "actionable": self.actionable,
            "viable": self.viable,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class LocatorResolution:
    """Selected locator plus the complete deterministic decision trail."""

    selected: Locator
    candidates: tuple[LocatorCandidate, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "selected": self.selected.to_dict(),
            "candidates": [item.to_dict() for item in self.candidates],
        }


class LocatorResolutionError(LookupError):
    """Raised when every candidate was deterministically rejected."""

    def __init__(self, candidates: tuple[LocatorCandidate, ...]) -> None:
        self.candidates = candidates
        attempted = ", ".join(
            f"{item.locator.strategy}={item.locator.value!r}"
            f" ({item.reason})"
            for item in candidates
        )
        super().__init__(f"Unable to resolve locator: {attempted}")


class LocatorEngine:
    """Generate, probe, rank, and select locators behind one small interface."""

    def resolve(
        self,
        requested: Locator,
        observation: Observation,
        probe: Callable[[Locator], LocatorProbe],
    ) -> LocatorResolution:
        proposals = self._proposals(requested, observation)
        evaluated: list[LocatorCandidate] = []
        for locator, source in proposals:
            try:
                facts = probe(locator)
                viable, reason = _viability(locator, facts)
            except Exception as exc:
                facts = LocatorProbe(0, False, False)
                viable, reason = False, f"probe failed: {exc}"
            evaluated.append(
                LocatorCandidate(
                    locator=locator,
                    source=source,
                    score=_score(locator, source, facts, viable),
                    count=facts.count,
                    visible=facts.visible,
                    enabled=facts.enabled,
                    actionable=facts.actionable,
                    viable=viable,
                    reason=reason,
                )
            )
        ranked = tuple(
            sorted(
                evaluated,
                key=lambda item: item.score,
                reverse=True,
            )
        )
        selected = next((item for item in ranked if item.viable), None)
        if selected is None:
            raise LocatorResolutionError(ranked)
        return LocatorResolution(selected.locator, ranked)

    def _proposals(
        self,
        requested: Locator,
        observation: Observation,
    ) -> tuple[tuple[Locator, str], ...]:
        proposals: list[tuple[Locator, str]] = [
            (item, "requested") for item in _flatten(requested)
        ]
        for element in observation.interactive_elements:
            if not _matches_requested(element, requested):
                continue
            proposals.extend(_from_element(element))
        unique: list[tuple[Locator, str]] = []
        seen: set[tuple[object, ...]] = set()
        for locator, source in proposals:
            key = (
                locator.strategy,
                locator.value,
                locator.name,
                locator.exact,
                locator.nth,
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(
                (
                    Locator(
                        locator.strategy,
                        locator.value,
                        name=locator.name,
                        exact=locator.exact,
                        nth=locator.nth,
                    ),
                    source,
                )
            )
        return tuple(unique)


def _flatten(locator: Locator) -> tuple[Locator, ...]:
    items = [locator]
    for fallback in locator.fallbacks:
        items.extend(_flatten(fallback))
    return tuple(items)


def _matches_requested(
    element: dict[str, JsonValue],
    requested: Locator,
) -> bool:
    target = (
        requested.name
        if requested.strategy == "role" and requested.name
        else requested.value
    )
    normalized = target.strip().casefold()
    if not normalized:
        return False
    values = (
        element.get("name"),
        element.get("label"),
        element.get("placeholder"),
        element.get("text"),
        element.get("test_id"),
    )
    return any(
        isinstance(value, str)
        and (
            value.strip().casefold() == normalized
            or (
                not requested.exact
                and normalized in value.strip().casefold()
            )
        )
        for value in values
    )


def _from_element(
    element: dict[str, JsonValue],
) -> list[tuple[Locator, str]]:
    proposals: list[tuple[Locator, str]] = []
    test_id = _text(element.get("test_id"))
    role = _text(element.get("role"))
    name = _text(element.get("name"))
    label = _text(element.get("label"))
    placeholder = _text(element.get("placeholder"))
    text = _text(element.get("text"))
    if test_id:
        proposals.append(
            (Locator("test_id", test_id), "observation:test_id")
        )
    if role and name:
        proposals.append(
            (
                Locator("role", role, name=name),
                "observation:accessible_role",
            )
        )
    if label:
        proposals.append((Locator("label", label), "observation:label"))
    if placeholder:
        proposals.append(
            (
                Locator("placeholder", placeholder),
                "observation:placeholder",
            )
        )
    if text:
        proposals.append((Locator("text", text), "observation:text"))
    return proposals


def _text(value: JsonValue) -> str:
    return value.strip() if isinstance(value, str) else ""


def _viability(
    locator: Locator,
    facts: LocatorProbe,
) -> tuple[bool, str]:
    if facts.count < 1:
        return False, "matched no elements"
    if locator.nth is None and facts.count != 1:
        return False, f"matched {facts.count} elements"
    if locator.nth is not None and locator.nth >= facts.count:
        return False, f"nth({locator.nth}) exceeds {facts.count} matches"
    if not facts.visible:
        return False, "element is not visible"
    if not facts.enabled:
        return False, "element is disabled"
    if not facts.actionable:
        return False, "element does not receive pointer actions"
    return True, "unique visible enabled match"


def _score(
    locator: Locator,
    source: str,
    facts: LocatorProbe,
    viable: bool,
) -> int:
    strategy_score = {
        "test_id": 100,
        "role": 90 if locator.name else 70,
        "label": 80,
        "placeholder": 75,
        "text": 60,
        "css": 40,
        "selector": 40,
        "xpath": 30,
    }.get(locator.strategy, 20)
    return (
        strategy_score
        + (5 if source == "requested" else 0)
        + (20 if facts.count == 1 else 0)
        + (10 if facts.visible else 0)
        + (5 if facts.enabled else 0)
        + (10 if facts.actionable else 0)
        + (0 if viable else -200)
    )


__all__ = [
    "LocatorCandidate",
    "LocatorEngine",
    "LocatorProbe",
    "LocatorResolution",
    "LocatorResolutionError",
]
