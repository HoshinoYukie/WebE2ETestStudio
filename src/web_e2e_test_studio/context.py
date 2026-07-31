"""Rich, navigation-safe UI context capture for planners and verifiers."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from web_e2e_test_studio.domain import (
    JsonValue,
    Observation,
    utc_now,
)


class ContextPurpose(StrEnum):
    """Why a caller needs context, which controls cache freshness."""

    PLANNING = "planning"
    INSIGHT = "insight"
    PREVIEW = "preview"


@dataclass(frozen=True)
class ContextRequest:
    purpose: ContextPurpose = ContextPurpose.PLANNING
    force_refresh: bool = False
    include_screenshot: bool = True


class ObservationProvider(Protocol):
    def observe(self) -> Observation: ...


@runtime_checkable
class ContextProvider(Protocol):
    def capture_context(
        self, request: ContextRequest | None = None
    ) -> UIContext: ...


@dataclass(frozen=True)
class FrameContext:
    url: str
    name: str = ""

    def to_dict(self) -> dict[str, JsonValue]:
        return {"url": self.url, "name": self.name}


@dataclass(frozen=True)
class TabContext:
    url: str
    title: str = ""
    active: bool = False

    def to_dict(self) -> dict[str, JsonValue]:
        return {"url": self.url, "title": self.title, "active": self.active}


@dataclass(frozen=True)
class UIContext(Observation):
    """Complete captured UI state with a legacy Observation view."""

    active_page_index: int = 0
    page_count: int = 1
    tabs: tuple[TabContext, ...] = ()
    frames: tuple[FrameContext, ...] = ()
    fingerprint: str = ""
    previous_fingerprint: str = ""
    changed_since_previous: bool = False
    captured_at: str = field(default_factory=utc_now)
    frozen: bool = False

    def to_observation(self) -> Observation:
        return Observation(
            url=self.url,
            title=self.title,
            screenshot_path=self.screenshot_path,
            viewport=dict(self.viewport),
            interactive_elements=tuple(
                dict(item) for item in self.interactive_elements
            ),
            page_text=self.page_text,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        data = super().to_dict()
        data.update(
            {
                "active_page_index": self.active_page_index,
                "page_count": self.page_count,
                "tabs": [tab.to_dict() for tab in self.tabs],
                "frames": [frame.to_dict() for frame in self.frames],
                "fingerprint": self.fingerprint,
                "previous_fingerprint": self.previous_fingerprint,
                "changed_since_previous": self.changed_since_previous,
                "captured_at": self.captured_at,
                "frozen": self.frozen,
            }
        )
        return data


def capture_observation(
    observer: ObservationProvider | ContextProvider,
    request: ContextRequest,
) -> Observation:
    """Use rich context when an observer provides it, else its legacy seam."""
    if isinstance(observer, ContextProvider):
        context = observer.capture_context(request)
        if not isinstance(context, Observation):
            raise TypeError("capture_context() must return an Observation")
        return context
    observation = observer.observe()
    if not isinstance(observation, Observation):
        raise TypeError("observe() must return an Observation")
    return observation


class ContextEngine:
    """Hide capture, retry, caching, sanitization, and fingerprinting."""

    def __init__(
        self,
        page_provider: Callable[[], Any],
        *,
        artifact_dir: str | Path | None = None,
        max_page_text: int = 20_000,
        max_elements: int = 300,
        retry_count: int = 3,
        navigation_timeout_ms: int = 5_000,
        planning_cache_ttl_ms: int = 1_000,
        clock: Callable[[], float] = time.monotonic,
        max_tabs: int = 20,
        max_frames: int = 100,
        max_screenshot_bytes: int = 12_000_000,
        sensitive_selectors: tuple[str, ...] = (
            'input[type="password"]',
            'input[autocomplete="current-password"]',
            'input[autocomplete="new-password"]',
            'input[autocomplete="one-time-code"]',
            'input[name*="token" i]',
            'input[name*="secret" i]',
            'input[name*="api_key" i]',
            'input[name*="card" i]',
            "[data-sensitive]",
            "[data-private]",
        ),
    ) -> None:
        if (
            max_page_text < 1
            or max_elements < 1
            or retry_count < 1
            or planning_cache_ttl_ms < 0
            or max_tabs < 1
            or max_frames < 1
            or max_screenshot_bytes < 1
        ):
            raise ValueError("Context limits and retry_count must be positive")
        self._page_provider = page_provider
        self._artifact_dir = (
            Path(artifact_dir).resolve() if artifact_dir is not None else None
        )
        self._max_page_text = max_page_text
        self._max_elements = max_elements
        self._retry_count = retry_count
        self._navigation_timeout_ms = navigation_timeout_ms
        self._planning_cache_ttl_ms = planning_cache_ttl_ms
        self._clock = clock
        self._max_tabs = max_tabs
        self._max_frames = max_frames
        self._max_screenshot_bytes = max_screenshot_bytes
        self._sensitive_selectors = sensitive_selectors
        self._capture_number = 0
        self._cached: UIContext | None = None
        self._cached_at = 0.0
        self._last_captured: UIContext | None = None
        self._frozen: UIContext | None = None

    def capture(self, request: ContextRequest | None = None) -> UIContext:
        request = request or ContextRequest()
        if self._frozen is not None:
            return self._frozen
        cached = self._cached
        screenshot_expected = (
            request.include_screenshot and self._artifact_dir is not None
        )
        should_reuse = (
            request.purpose is ContextPurpose.PLANNING
            and not request.force_refresh
            and cached is not None
            and (self._clock() - self._cached_at) * 1000
            <= self._planning_cache_ttl_ms
            and bool(cached.screenshot_path) is screenshot_expected
        )
        if should_reuse and cached is not None:
            return cached
        page = self._page_provider()
        if page is None:
            raise RuntimeError("No active page is available")
        for attempt in range(self._retry_count):
            try:
                captured = self._capture(page, request)
                self._cached = captured
                self._cached_at = self._clock()
                self._last_captured = captured
                return captured
            except Exception as exc:
                if (
                    attempt == self._retry_count - 1
                    or not _is_navigation_context_error(exc)
                ):
                    raise
                with suppress(Exception):
                    page.wait_for_load_state(
                        "domcontentloaded",
                        timeout=self._navigation_timeout_ms,
                    )
                page = self._page_provider()
        raise RuntimeError("Context capture retry loop ended unexpectedly")

    def freeze(self) -> UIContext:
        context = self.capture(
            ContextRequest(ContextPurpose.INSIGHT, force_refresh=True)
        )
        frozen = replace(context, frozen=True)
        self._frozen = frozen
        return frozen

    def unfreeze(self) -> None:
        self._frozen = None
        self._cached = None

    def invalidate(self) -> None:
        """Discard reusable context after a deterministic action."""
        if self._frozen is None:
            self._cached = None
            self._cached_at = 0.0

    def reset(self) -> None:
        """Discard all state when the underlying browser session changes."""
        self._cached = None
        self._cached_at = 0.0
        self._last_captured = None
        self._frozen = None

    def _capture(self, page: Any, request: ContextRequest) -> UIContext:
        raw = page.evaluate(_CAPTURE_SCRIPT)
        snapshot = dict(raw) if isinstance(raw, dict) else {}
        viewport = snapshot.get("viewport", {})
        raw_elements = snapshot.get("interactive_elements", [])
        elements = tuple(
            _sanitize_element(item)
            for item in (
                raw_elements[: self._max_elements]
                if isinstance(raw_elements, list)
                else []
            )
            if isinstance(item, dict)
        )
        page_text = _sanitize_text(
            str(snapshot.get("page_text", ""))[: self._max_page_text]
        )
        url = _sanitize_url(str(page.url))
        title = _sanitize_inline(str(page.title()), limit=500)
        all_pages = _pages(page)
        active_page_index = _active_page_index(all_pages, page)
        pages = list(all_pages[: self._max_tabs])
        if page not in pages:
            pages[-1] = page
        tabs = tuple(
            TabContext(
                url=_sanitize_url(str(getattr(tab, "url", ""))),
                title=_sanitize_inline(_safe_title(tab), limit=500),
                active=tab is page,
            )
            for tab in pages
        )
        frames = tuple(
            FrameContext(
                url=_sanitize_url(str(getattr(frame, "url", ""))),
                name=_sanitize_inline(
                    str(getattr(frame, "name", "")),
                    limit=500,
                ),
            )
            for frame in list(getattr(page, "frames", ()))[: self._max_frames]
        )
        screenshot_path = ""
        if request.include_screenshot and self._artifact_dir is not None:
            self._artifact_dir.mkdir(parents=True, exist_ok=True)
            self._capture_number += 1
            screenshot = (
                self._artifact_dir
                / f"context-{self._capture_number:04d}.png"
            )
            options: dict[str, Any] = {
                "path": str(screenshot),
                "full_page": False,
            }
            locator = getattr(page, "locator", None)
            if callable(locator) and self._sensitive_selectors:
                options.update(
                    {
                        "mask": [
                            locator(",".join(self._sensitive_selectors))
                        ],
                        "mask_color": "#000000",
                    }
                )
            page.screenshot(**options)
            if screenshot.stat().st_size <= self._max_screenshot_bytes:
                screenshot_path = str(screenshot)
            else:
                with suppress(OSError):
                    screenshot.unlink()
        normalized_viewport = (
            {
                str(key): int(value)
                for key, value in viewport.items()
                if isinstance(value, (int, float))
            }
            if isinstance(viewport, dict)
            else {}
        )
        fingerprint = _fingerprint(
            url,
            title,
            normalized_viewport,
            elements,
            page_text,
            tabs,
            frames,
        )
        previous_fingerprint = (
            self._last_captured.fingerprint
            if self._last_captured is not None
            else ""
        )
        return UIContext(
            url=url,
            title=title,
            screenshot_path=screenshot_path,
            viewport=normalized_viewport,
            interactive_elements=elements,
            page_text=page_text,
            active_page_index=active_page_index,
            page_count=max(len(all_pages), 1),
            tabs=tabs,
            frames=frames,
            fingerprint=fingerprint,
            previous_fingerprint=previous_fingerprint,
            changed_since_previous=bool(
                previous_fingerprint
                and previous_fingerprint != fingerprint
            ),
        )


def _pages(page: Any) -> list[Any]:
    context = getattr(page, "context", None)
    pages = getattr(context, "pages", None)
    return list(pages) if isinstance(pages, (list, tuple)) else [page]


def _active_page_index(pages: list[Any], page: Any) -> int:
    with suppress(ValueError):
        return pages.index(page)
    return 0


def _safe_title(page: Any) -> str:
    with suppress(Exception):
        return str(page.title())
    return ""


def _sanitize_element(item: dict[str, Any]) -> dict[str, JsonValue]:
    sanitized: dict[str, JsonValue] = {
        str(key): (
            _sanitize_inline(value, limit=500)
            if isinstance(value, str)
            else value
        )
        for key, value in item.items()
        if value is None or isinstance(value, (bool, int, float, str))
    }
    if str(sanitized.get("input_type", "")).lower() == "password":
        sanitized["text"] = ""
        sanitized["value"] = ""
    return sanitized


_SECRET_LINE = re.compile(
    r"(?im)^.*(?:password|passwd|authorization|api[_ -]?key|"
    r"access[_ -]?token|secret|cookie)\s*[:=].*$"
)


def _sanitize_text(value: str) -> str:
    return _SECRET_LINE.sub("[REDACTED]", value)


_INLINE_SECRET = re.compile(
    r"(?i)(password|passwd|authorization|api[_ -]?key|access[_ -]?token|"
    r"refresh[_ -]?token|client[_ -]?secret|cookie)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def _sanitize_inline(value: str, *, limit: int) -> str:
    sanitized = _INLINE_SECRET.sub(r"\1\2[REDACTED]", value)
    return sanitized[:limit]


_SECRET_QUERY_KEYS = {
    "access_token",
    "api_key",
    "apikey",
    "auth",
    "authorization",
    "key",
    "password",
    "secret",
    "token",
}


def _sanitize_url(value: str) -> str:
    parsed = urlsplit(value)
    query = urlencode(
        [
            (
                key,
                "[REDACTED]"
                if key.lower() in _SECRET_QUERY_KEYS
                else item_value,
            )
            for key, item_value in parse_qsl(
                parsed.query, keep_blank_values=True
            )
        ]
    )
    hostname = parsed.hostname or ""
    if parsed.port is not None:
        hostname = f"{hostname}:{parsed.port}"
    netloc = (
        f"[REDACTED]@{hostname}"
        if parsed.username is not None or parsed.password is not None
        else parsed.netloc
    )
    fragment = parsed.fragment
    looks_like_jwt = fragment.count(".") == 2 and len(fragment) > 40
    if (
        any(secret in fragment.lower() for secret in _SECRET_QUERY_KEYS)
        or looks_like_jwt
        or len(fragment) > 100
    ):
        fragment = "[REDACTED]"
    return urlunsplit((parsed.scheme, netloc, parsed.path, query, fragment))


def _fingerprint(
    url: str,
    title: str,
    viewport: dict[str, int],
    elements: tuple[dict[str, JsonValue], ...],
    page_text: str,
    tabs: tuple[TabContext, ...],
    frames: tuple[FrameContext, ...],
) -> str:
    payload = {
        "url": url,
        "title": title,
        "viewport": viewport,
        "elements": elements,
        "page_text_digest": hashlib.sha256(
            page_text.encode("utf-8")
        ).hexdigest(),
        "tabs": [tab.to_dict() for tab in tabs],
        "frames": [frame.to_dict() for frame in frames],
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _is_navigation_context_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "execution context was destroyed" in message
        or "cannot find context with specified id" in message
        or "frame was detached" in message
        or "navigation interrupted" in message
        or "most likely because of a navigation" in message
        or "target page, context or browser has been closed" in message
        or "page closed" in message
    )


_CAPTURE_SCRIPT = """() => ({
  viewport: {
    width: window.innerWidth,
    height: window.innerHeight
  },
  interactive_elements: Array.from(document.querySelectorAll(
    'a,button,input,select,textarea,summary,[role],[data-testid],[contenteditable],[onclick]'
  )).slice(0, 1000).map((element) => {
    const inputType = element.getAttribute('type') || '';
    const isSecret = inputType.toLowerCase() === 'password';
    const label = element.labels?.[0]?.innerText?.trim().slice(0, 200) || '';
    const implicitRole = ({
      A: element.hasAttribute('href') ? 'link' : '',
      BUTTON: 'button',
      SELECT: 'combobox',
      TEXTAREA: 'textbox',
      SUMMARY: 'button'
    })[element.tagName] || (
      element.tagName === 'INPUT'
        ? ({checkbox: 'checkbox', radio: 'radio', button: 'button',
            submit: 'button', range: 'slider'}[inputType.toLowerCase()] || 'textbox')
        : ''
    );
    const text = isSecret ? '' : (element.innerText || '').trim().slice(0, 200);
    return {
      tag: element.tagName.toLowerCase(),
      role: element.getAttribute('role') || implicitRole,
      name: element.getAttribute('aria-label') || label ||
        element.getAttribute('alt') || element.getAttribute('title') ||
        element.getAttribute('placeholder') || text,
      description: element.getAttribute('aria-description') || '',
      label,
      placeholder: element.getAttribute('placeholder') || '',
      text,
      test_id: element.getAttribute('data-testid') || '',
      input_type: inputType,
      disabled: Boolean(element.disabled),
      checked: typeof element.checked === 'boolean' ? element.checked : null,
      expanded: element.getAttribute('aria-expanded'),
      selected: element.getAttribute('aria-selected'),
      visible: Boolean(element.getClientRects().length)
    };
  }),
  page_text: (document.body ? document.body.innerText : '').slice(0, 100000)
})"""


__all__ = [
    "ContextEngine",
    "ContextPurpose",
    "ContextRequest",
    "ContextProvider",
    "FrameContext",
    "TabContext",
    "UIContext",
    "ObservationProvider",
    "capture_observation",
]
