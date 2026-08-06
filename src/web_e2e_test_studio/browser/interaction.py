"""Frame-aware deterministic element resolution behind one interaction seam."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from web_e2e_test_studio.browser.locator import (
    LocatorEngine,
    LocatorProbe,
    LocatorResolution,
)
from web_e2e_test_studio.domain import ElementTarget, FrameRef, Locator, Observation


@dataclass(frozen=True)
class ResolvedCandidate:
    locator: Locator
    handle: Any
    frame_path: tuple[FrameRef, ...] = ()


@dataclass(frozen=True)
class InteractionResolution:
    """Ranked executable handles plus their auditable locator decision."""

    candidates: tuple[ResolvedCandidate, ...]
    locator_resolution: LocatorResolution

    @property
    def primary(self) -> Any:
        return self.candidates[0].handle


class InteractionEngine:
    """Resolve portable targets across tabs and frames with deterministic fallbacks."""

    def __init__(
        self,
        page_provider: Callable[[], Any],
        locator_builder: Callable[[Any, Locator], Any],
        *,
        locator_engine: LocatorEngine | None = None,
    ) -> None:
        self._page_provider = page_provider
        self._locator_builder = locator_builder
        self._locator_engine = locator_engine or LocatorEngine()

    def resolve(
        self,
        target: ElementTarget,
        observation: Observation,
        *,
        require_actionable: bool = False,
    ) -> InteractionResolution:
        page = self._page_provider()
        scopes = self._scopes(page, target.frame_path)
        handles: dict[Locator, tuple[Any, tuple[FrameRef, ...]]] = {}

        def probe(candidate: Locator) -> LocatorProbe:
            matches: list[tuple[Any, int, tuple[FrameRef, ...]]] = []
            count = 0
            for scope, frame_path in scopes:
                handle = self._locator_builder(scope, candidate)
                scope_count = int(handle.count())
                if scope_count < 1:
                    continue
                matches.append((handle, scope_count, frame_path))
                count += scope_count
            if count < 1:
                return LocatorProbe(0, False, False, False)
            handle, frame_path = _select_scoped_handle(matches, candidate.nth)
            handles[candidate] = (handle, frame_path)
            actionable = True
            if require_actionable and _is_playwright_locator(handle):
                try:
                    handle.click(trial=True, timeout=1_500)
                except Exception:
                    actionable = False
            is_visible = getattr(handle, "is_visible", None)
            is_enabled = getattr(handle, "is_enabled", None)
            return LocatorProbe(
                count=count,
                visible=bool(is_visible()) if callable(is_visible) else True,
                enabled=bool(is_enabled()) if callable(is_enabled) else True,
                actionable=actionable,
            )

        locator_resolution = self._locator_engine.resolve(
            target.locator,
            observation,
            probe,
        )
        ranked = tuple(
            ResolvedCandidate(item.locator, handles[item.locator][0], handles[item.locator][1])
            for item in locator_resolution.candidates
            if item.viable and item.locator in handles
        )
        if not ranked:
            raise LookupError("Locator resolution produced no executable target")
        return InteractionResolution(ranked, locator_resolution)

    @staticmethod
    def _scopes(
        page: Any,
        frame_path: tuple[FrameRef, ...],
    ) -> tuple[tuple[Any, tuple[FrameRef, ...]], ...]:
        if frame_path:
            scope = page
            traversed: list[FrameRef] = []
            for frame_ref in frame_path:
                scope = _resolve_frame_scope(scope, frame_ref)
                traversed.append(frame_ref)
            return ((scope, tuple(traversed)),)
        frames = tuple(getattr(page, "frames", ()))
        return tuple((frame, ()) for frame in frames) or ((page, ()),)


def _resolve_frame_scope(scope: Any, frame: FrameRef) -> Any:
    if frame.selector:
        frame_locator = getattr(scope, "frame_locator", None)
        if callable(frame_locator):
            selected = frame_locator(frame.selector)
            return selected.nth(frame.index) if frame.index is not None else selected
    child_frames = tuple(getattr(scope, "child_frames", ()))
    if not child_frames and hasattr(scope, "main_frame"):
        child_frames = tuple(getattr(scope.main_frame, "child_frames", ()))
    candidates = child_frames
    if frame.name:
        candidates = tuple(
            item
            for item in candidates
            if str(getattr(item, "name", "")) == frame.name
        )
    if frame.url_hint:
        candidates = tuple(
            item
            for item in candidates
            if frame.url_hint in str(getattr(item, "url", ""))
        )
    index = frame.index or 0
    if index >= len(candidates):
        raise LookupError(f"Frame path could not be resolved: {frame}")
    return candidates[index]


def _select_scoped_handle(
    matches: list[tuple[Any, int, tuple[FrameRef, ...]]],
    nth: int | None,
) -> tuple[Any, tuple[FrameRef, ...]]:
    if nth is None:
        handle, _count, frame_path = matches[0]
        return handle, frame_path
    remaining = nth
    for handle, count, frame_path in matches:
        if remaining < count:
            return handle.nth(remaining), frame_path
        remaining -= count
    handle, count, frame_path = matches[-1]
    return handle.nth(count), frame_path


def _is_playwright_locator(target: Any) -> bool:
    module = str(getattr(target.__class__, "__module__", ""))
    return module.startswith("playwright.")


__all__ = ["InteractionEngine", "InteractionResolution", "ResolvedCandidate"]
