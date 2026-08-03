"""Compile browser-side manual interactions into deterministic actions."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

from web_e2e_test_studio.domain import Action, ActionChannel, Locator

_EVENT_QUEUE = "__WEB_E2E_TEST_STUDIO_EVENTS__"
_CONSOLE_PREFIX = "__WEB_E2E_TEST_STUDIO_EVENT__"


@dataclass(frozen=True)
class _PageEvent:
    page: Any
    event: Mapping[str, Any]
    sequence: int


class ManualEventCompiler:
    """Translate events captured in the page into model-independent actions."""

    def __init__(self) -> None:
        self._password_number = 0

    def compile(self, events: Iterable[Mapping[str, Any]]) -> list[Action]:
        actions: list[Action] = []
        for event in events:
            event_type = event.get("type")
            if event_type == "navigation":
                url = event.get("url")
                if url:
                    actions.append(
                        Action(
                            name="navigate",
                            channel=ActionChannel.BROWSER,
                            arguments={"url": str(url)},
                        )
                    )
                continue
            if event_type not in {"click", "input", "change"}:
                continue
            locator = self._locator_for(event)
            if locator is None:
                continue
            if event_type == "click":
                action = Action(
                    name="click",
                    channel=ActionChannel.BROWSER,
                    locator=locator,
                )
            else:
                raw_target = event.get("target", {})
                input_type = (
                    str(raw_target.get("input_type", raw_target.get("type", "")))
                    if isinstance(raw_target, Mapping)
                    else ""
                )
                tag = (
                    str(raw_target.get("tag", "")).lower()
                    if isinstance(raw_target, Mapping)
                    else ""
                )
                if event_type == "change" and tag == "select":
                    action = Action(
                        name="select",
                        channel=ActionChannel.BROWSER,
                        arguments={"value": str(event.get("value", ""))},
                        locator=locator,
                    )
                elif event_type == "change" and input_type.lower() in {
                    "checkbox",
                    "radio",
                }:
                    action = Action(
                        name="check",
                        channel=ActionChannel.BROWSER,
                        arguments={"checked": bool(event.get("checked", True))},
                        locator=locator,
                    )
                else:
                    text = str(event.get("value", ""))
                    if input_type.lower() == "password":
                        self._password_number += 1
                        text = (
                            f"${{ENV:RECORDED_PASSWORD_"
                            f"{self._password_number}}}"
                        )
                    action = Action(
                        name="fill",
                        channel=ActionChannel.BROWSER,
                        arguments={"text": text},
                        locator=locator,
                    )
                    if (
                        actions
                        and actions[-1].name == "fill"
                        and actions[-1].locator == locator
                    ):
                        actions[-1] = action
                        continue
            actions.append(action)
        return actions

    @staticmethod
    def _locator_for(event: Mapping[str, Any]) -> Locator | None:
        raw_target = event.get("target", {})
        if not isinstance(raw_target, Mapping):
            return None

        nth_value = raw_target.get("nth")
        nth = int(nth_value) if isinstance(nth_value, int) else None
        candidates: list[Locator] = []
        role = raw_target.get("role")
        name = raw_target.get("name")
        if role and name:
            candidates.append(
                Locator("role", str(role), name=str(name), nth=nth)
            )
        for strategy, key in (
            ("test_id", "test_id"),
            ("label", "label"),
            ("text", "text"),
            ("css", "css"),
            ("xpath", "xpath"),
        ):
            value = raw_target.get(key)
            if value:
                candidates.append(Locator(strategy, str(value), nth=nth))
        if not candidates:
            return None
        primary, *fallbacks = candidates
        return Locator(
            strategy=primary.strategy,
            value=primary.value,
            name=primary.name,
            exact=primary.exact,
            nth=primary.nth,
            fallbacks=tuple(fallbacks),
        )


class BrowserEventRecorder:
    """Install a page-local event queue and drain it without using a model."""

    def __init__(self, compiler: ManualEventCompiler | None = None) -> None:
        self._compiler = compiler or ManualEventCompiler()
        self._console_events: list[_PageEvent] = []
        self._installed_pages: set[int] = set()
        self._pages: dict[int, Any] = {}
        self._page_refs: dict[int, str] = {}
        self._page_openers: dict[int, int | None] = {}
        self._page_created_sequence: dict[int, int] = {}
        self._pending_popups: list[int] = []
        self._next_popup_number = 1
        self._sequence = 0
        self._main_bound = False
        self._replay_active_ref = "main"

    def install(self, page: Any) -> None:
        """Install capture on the current page and future navigations."""
        identity = id(page)
        if identity in self._installed_pages:
            return
        page.on(
            "console",
            lambda message, source=page: self._capture_console_message(
                source, message
            ),
        )
        script = self.installation_script()
        page.add_init_script(script=script)
        # The init script will install after a popup's initial navigation.
        with suppress(Exception):
            page.evaluate(script)
        self._installed_pages.add(identity)
        self._sequence += 1
        self._pages[identity] = page
        self._page_created_sequence[identity] = self._sequence
        if not self._page_refs:
            self._page_refs[identity] = "main"
            self._page_openers[identity] = None
        else:
            self._page_refs[identity] = f"popup_{self._next_popup_number}"
            self._next_popup_number += 1
            self._page_openers[identity] = self._opener_identity(page)
            self._pending_popups.append(identity)

    @staticmethod
    def installation_script() -> str:
        """Return the init script that captures replayable manual events."""
        return rf"""(() => {{
  const queueName = '{_EVENT_QUEUE}';
  if (window.__WEB_E2E_TEST_STUDIO_RECORDER_INSTALLED__) return;
  window.__WEB_E2E_TEST_STUDIO_RECORDER_INSTALLED__ = true;
  window[queueName] = window[queueName] || [];

  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const cssEscape = (value) => {{
    if (window.CSS && CSS.escape) return CSS.escape(value);
    return String(value).replace(/[^a-zA-Z0-9_-]/g, '\\$&');
  }};
  const cssPath = (element) => {{
    if (element.id) return `#${{cssEscape(element.id)}}`;
    const testId = element.getAttribute('data-testid');
    if (testId) return `[data-testid="${{testId.replace(/"/g, '\\"')}}"]`;
    const parts = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE) {{
      let part = current.tagName.toLowerCase();
      const siblings = current.parentElement
        ? Array.from(current.parentElement.children).filter(
            (item) => item.tagName === current.tagName
          )
        : [];
      if (siblings.length > 1) {{
        part += `:nth-of-type(${{siblings.indexOf(current) + 1}})`;
      }}
      parts.unshift(part);
      current = current.parentElement;
      if (parts.length >= 6) break;
    }}
    return parts.join(' > ');
  }};
  const xpath = (element) => {{
    if (element.id) return `//*[@id="${{element.id.replace(/"/g, '\\"')}}"]`;
    const parts = [];
    let current = element;
    while (current && current.nodeType === Node.ELEMENT_NODE) {{
      const tag = current.tagName.toLowerCase();
      const siblings = current.parentElement
        ? Array.from(current.parentElement.children).filter(
            (item) => item.tagName === current.tagName
          )
        : [];
      parts.unshift(`${{tag}}[${{Math.max(1, siblings.indexOf(current) + 1)}}]`);
      current = current.parentElement;
    }}
    return `/${{parts.join('/')}}`;
  }};
  const implicitRole = (element) => {{
    const tag = element.tagName.toLowerCase();
    const type = (element.getAttribute('type') || '').toLowerCase();
    if (tag === 'button') return 'button';
    if (tag === 'a' && element.hasAttribute('href')) return 'link';
    if (tag === 'select') return 'combobox';
    if (tag === 'textarea') return 'textbox';
    if (tag === 'input' && ['button', 'submit', 'reset'].includes(type)) {{
      return 'button';
    }}
    if (tag === 'input' && type === 'checkbox') return 'checkbox';
    if (tag === 'input' && type === 'radio') return 'radio';
    if (tag === 'input' && !['hidden', 'file'].includes(type)) return 'textbox';
    return '';
  }};
  const describe = (source) => {{
    const element = source.closest(
      'button,a,input,select,textarea,[role],[data-testid],[contenteditable]'
    ) || source;
    const labels = element.labels ? Array.from(element.labels) : [];
    const label = clean(labels.map((item) => item.innerText).join(' '));
    const name = clean(
      element.getAttribute('aria-label') ||
      label ||
      element.getAttribute('alt') ||
      element.getAttribute('placeholder') ||
      element.innerText ||
      element.value
    );
    const selector = cssPath(element);
    const matches = selector ? document.querySelectorAll(selector) : [];
    const nth = matches.length > 1 ? Array.from(matches).indexOf(element) : null;
    return {{
      role: element.getAttribute('role') || implicitRole(element),
      name,
      test_id: element.getAttribute('data-testid') || '',
      label,
      text: clean(element.innerText),
      css: selector,
      xpath: xpath(element),
      nth,
      tag: element.tagName.toLowerCase(),
      input_type: (element.getAttribute('type') || '').toLowerCase()
    }};
  }};
  const emit = (event) => {{
    const recorded = {{ ...event, timestamp: Date.now() }};
    window[queueName].push(recorded);
    console.debug('{_CONSOLE_PREFIX}' + JSON.stringify(recorded));
  }};

  document.addEventListener('click', (event) => {{
    if (event.target instanceof Element) {{
      emit({{ type: 'click', target: describe(event.target) }});
    }}
  }}, true);
  document.addEventListener('input', (event) => {{
    if (!(event.target instanceof Element)) return;
    const target = describe(event.target);
    const value = target.input_type === 'password' ? '' : event.target.value;
    emit({{ type: 'input', target, value }});
  }}, true);
  document.addEventListener('change', (event) => {{
    if (!(event.target instanceof Element)) return;
    const target = describe(event.target);
    const value = target.input_type === 'password' ? '' : event.target.value;
    emit({{
      type: 'change',
      target,
      value,
      checked: Boolean(event.target.checked)
    }});
  }}, true);

  const emitNavigation = () => emit({{
    type: 'navigation',
    url: window.location.href
  }});
  for (const method of ['pushState', 'replaceState']) {{
    const original = history[method];
    history[method] = function(...args) {{
      const result = original.apply(this, args);
      emitNavigation();
      return result;
    }};
  }}
  window.addEventListener('popstate', emitNavigation);
  window.addEventListener('hashchange', emitNavigation);
}})();"""

    def drain(self, page: Any) -> list[Action]:
        """Read and clear the browser queue, returning compiled actions."""
        wait_for_timeout = getattr(page, "wait_for_timeout", None)
        if callable(wait_for_timeout):
            wait_for_timeout(0)
        raw_events = page.evaluate(
            rf"""() => {{
  const queue = window.{_EVENT_QUEUE} || [];
  return queue.splice(0, queue.length);
}}"""
        )
        queued = (
            [item for item in raw_events if isinstance(item, Mapping)]
            if isinstance(raw_events, list)
            else []
        )
        page_events = [
            item.event for item in self._console_events if item.page is page
        ]
        events = _deduplicate_events([*page_events, *queued])
        self._console_events = [
            item for item in self._console_events if item.page is not page
        ]
        return self._compiler.compile(events)

    def drain_all(self, pages: Iterable[Any] | None = None) -> list[Action]:
        """Compile one chronological, replayable action stream across tabs."""
        live_pages = list(pages) if pages is not None else list(self._pages.values())
        queued_events: list[_PageEvent] = []
        for page in live_pages:
            wait_for_timeout = getattr(page, "wait_for_timeout", None)
            if callable(wait_for_timeout):
                wait_for_timeout(0)
            try:
                raw_events = page.evaluate(
                    rf"""() => {{
  const queue = window.{_EVENT_QUEUE} || [];
  return queue.splice(0, queue.length);
}}"""
                )
            except Exception:
                raw_events = []
            if isinstance(raw_events, list):
                for event in raw_events:
                    if isinstance(event, Mapping):
                        self._sequence += 1
                        queued_events.append(
                            _PageEvent(page, event, self._sequence)
                        )

        timeline = self._deduplicate_page_events(
            [*self._console_events, *queued_events]
        )
        self._console_events.clear()
        entries: list[tuple[int, int, Action]] = []
        for captured in timeline:
            for action in self._compiler.compile((captured.event,)):
                entry = (captured.sequence, id(captured.page), action)
                if (
                    entries
                    and action.name == "fill"
                    and entries[-1][1] == entry[1]
                    and entries[-1][2].name == "fill"
                    and entries[-1][2].locator == action.locator
                ):
                    entries[-1] = entry
                else:
                    entries.append(entry)

        popup_for_click: dict[int, int] = {}
        consumed_clicks: set[int] = set()
        for popup_id in self._pending_popups:
            opener_id = self._page_openers.get(popup_id)
            created_sequence = self._page_created_sequence.get(popup_id, 0)
            if opener_id is None:
                continue
            for index in range(len(entries) - 1, -1, -1):
                sequence, page_id, action = entries[index]
                if (
                    index not in consumed_clicks
                    and sequence < created_sequence
                    and page_id == opener_id
                    and action.name == "click"
                ):
                    popup_for_click[index] = popup_id
                    consumed_clicks.add(index)
                    break

        actions: list[Action] = []
        if not self._main_bound and self._page_refs:
            actions.append(
                Action(
                    "bind_page",
                    ActionChannel.BROWSER,
                    arguments={"page_ref": "main"},
                )
            )
            self._main_bound = True
        active_ref = self._replay_active_ref
        for index, (_sequence, page_id, action) in enumerate(entries):
            page_ref = self._page_refs.get(page_id, "main")
            matched_popup_id = popup_for_click.get(index)
            if matched_popup_id is not None:
                popup_ref = self._page_refs[matched_popup_id]
                if active_ref != page_ref:
                    actions.append(self._switch_action(page_ref))
                actions.append(
                    Action(
                        "open_popup",
                        ActionChannel.BROWSER,
                        arguments={"page_ref": popup_ref},
                        locator=action.locator,
                    )
                )
                active_ref = popup_ref
                continue
            if active_ref != page_ref:
                actions.append(self._switch_action(page_ref))
                active_ref = page_ref
            actions.append(action)
        self._pending_popups.clear()
        self._replay_active_ref = active_ref
        return actions

    def _capture_console_message(self, page: Any, message: Any) -> None:
        raw_text = getattr(message, "text", "")
        text = raw_text() if callable(raw_text) else str(raw_text)
        if not text.startswith(_CONSOLE_PREFIX):
            return
        try:
            event = json.loads(text.removeprefix(_CONSOLE_PREFIX))
        except json.JSONDecodeError:
            return
        if isinstance(event, Mapping):
            self._sequence += 1
            self._console_events.append(_PageEvent(page, event, self._sequence))

    @staticmethod
    def _switch_action(page_ref: str) -> Action:
        return Action(
            "switch_page",
            ActionChannel.BROWSER,
            arguments={"page_ref": page_ref},
        )

    @staticmethod
    def _opener_identity(page: Any) -> int | None:
        opener = getattr(page, "opener", None)
        if not callable(opener):
            return None
        try:
            parent = opener()
        except Exception:
            return None
        return id(parent) if parent is not None else None

    @staticmethod
    def _deduplicate_page_events(events: list[_PageEvent]) -> list[_PageEvent]:
        unique: list[_PageEvent] = []
        seen: set[tuple[int, str]] = set()
        for item in sorted(events, key=lambda captured: captured.sequence):
            signature = (
                id(item.page),
                json.dumps(item.event, sort_keys=True, ensure_ascii=False),
            )
            if signature in seen:
                continue
            seen.add(signature)
            unique.append(item)
        return unique


def _deduplicate_events(
    events: Iterable[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    unique: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        signature = json.dumps(event, sort_keys=True, ensure_ascii=False)
        if signature in seen:
            continue
        seen.add(signature)
        unique.append(event)
    return unique


__all__ = ["BrowserEventRecorder", "ManualEventCompiler"]
