"""Compile browser-side manual interactions into deterministic actions."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from web_e2e_test_studio.domain import Action, ActionChannel, Locator

_EVENT_QUEUE = "__WEB_E2E_TEST_STUDIO_EVENTS__"
_CONSOLE_PREFIX = "__WEB_E2E_TEST_STUDIO_EVENT__"


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
        self._console_events: list[Mapping[str, Any]] = []
        self._installed_pages: set[int] = set()

    def install(self, page: Any) -> None:
        """Install capture on the current page and future navigations."""
        identity = id(page)
        if identity in self._installed_pages:
            return
        page.on("console", self._capture_console_message)
        script = self.installation_script()
        page.add_init_script(script=script)
        page.evaluate(script)
        self._installed_pages.add(identity)

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
        events = _deduplicate_events([*self._console_events, *queued])
        self._console_events.clear()
        return self._compiler.compile(events)

    def _capture_console_message(self, message: Any) -> None:
        raw_text = getattr(message, "text", "")
        text = raw_text() if callable(raw_text) else str(raw_text)
        if not text.startswith(_CONSOLE_PREFIX):
            return
        try:
            event = json.loads(text.removeprefix(_CONSOLE_PREFIX))
        except json.JSONDecodeError:
            return
        if isinstance(event, Mapping):
            self._console_events.append(event)


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
