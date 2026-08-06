"""Synchronous Playwright device used for authoring and deterministic replay."""

from __future__ import annotations

import base64
import threading
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web_e2e_test_studio.agent.actions import (
    ActionSpec,
    resolve_wait_milliseconds,
)
from web_e2e_test_studio.agent.control import (
    ActionCancelledError,
    ActionExecutionContext,
)
from web_e2e_test_studio.browser.interaction import (
    InteractionEngine,
    InteractionResolution,
)
from web_e2e_test_studio.browser.locator import LocatorEngine, LocatorResolution
from web_e2e_test_studio.context import (
    ContextEngine,
    ContextPurpose,
    ContextRequest,
    UIContext,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    ActionStatus,
    ElementTarget,
    JsonValue,
    Locator,
    Observation,
)


def _default_playwright_factory() -> Any:
    from playwright.sync_api import sync_playwright

    return sync_playwright()


@dataclass(frozen=True)
class BrowserPageInfo:
    """Stable UI-facing identity for one tab or popup."""

    id: str
    url: str
    title: str
    active: bool
    opener_id: str = ""


class PlaywrightDevice:
    """Own a Playwright browser, context, and active page."""

    def __init__(
        self,
        *,
        browser_name: str = "chromium",
        headless: bool = True,
        viewport: dict[str, int] | None = None,
        artifact_dir: str | Path | None = None,
        playwright_factory: Callable[[], Any] | None = None,
        page: Any | None = None,
        locator_engine: LocatorEngine | None = None,
    ) -> None:
        self._browser_name = browser_name
        self._headless = headless
        self._viewport = viewport or {"width": 1280, "height": 720}
        self._artifact_dir = Path(artifact_dir).resolve() if artifact_dir is not None else None
        self._playwright_factory = playwright_factory or _default_playwright_factory
        self._playwright: Any | None = None
        self._browser: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = page
        self._owns_browser = page is None
        self._locator_engine = locator_engine or LocatorEngine()
        self._interaction_engine = InteractionEngine(
            self._require_page,
            lambda scope, locator: self._build_locator(locator, scope=scope),
            locator_engine=self._locator_engine,
        )
        self._last_observation = Observation()
        self._last_locator_resolution: LocatorResolution | None = None
        self._context_engine = ContextEngine(
            lambda: self._page,
            artifact_dir=self._artifact_dir,
        )
        self._page_ids: dict[int, str] = {}
        self._pages_by_id: dict[str, Any] = {}
        self._next_page_number = 1
        self._page_listeners: list[Callable[[Any], None]] = []
        self._preview_listeners: list[Callable[[Observation, str], None]] = []
        self._cdp_sessions: dict[int, Any] = {}
        self._page_refs: dict[str, Any] = {}
        self._preview_frame_number = 0
        self._preview_frame_paths: deque[Path] = deque()
        self._preview_frame_lock = threading.Lock()
        if page is not None:
            self._context = getattr(page, "context", None)
            self._install_page_tracking(self._context)

    @property
    def page(self) -> Any | None:
        return self._page

    def pages(self) -> tuple[BrowserPageInfo, ...]:
        """Return every live tab/popup with a stable session-local ID."""
        browser_pages = list(getattr(self._context, "pages", ()))
        for page in browser_pages:
            self._register_page(page)
        return tuple(
            BrowserPageInfo(
                id=self._page_ids[id(page)],
                url=str(getattr(page, "url", "")),
                title=self._safe_page_title(page),
                active=page is self._page,
                opener_id=self._opener_id(page),
            )
            for page in browser_pages
            if not self._page_is_closed(page)
        )

    def add_page_listener(self, listener: Callable[[Any], None]) -> None:
        """Notify a recorder or observer for existing and future pages."""
        self._page_listeners.append(listener)
        for page in list(getattr(self._context, "pages", ())):
            listener(page)

    def add_preview_listener(self, listener: Callable[[Observation, str], None]) -> None:
        """Receive push-based Chromium screencast frames when supported."""
        self._preview_listeners.append(listener)
        for page in list(getattr(self._context, "pages", ())):
            self._start_screencast(page)

    def activate_page(self, page_id: str) -> None:
        """Select one registered tab/popup for subsequent Agent actions."""
        page = self._pages_by_id.get(page_id)
        if page is None or self._page_is_closed(page):
            raise ValueError(f"Browser page is not available: {page_id}")
        bring_to_front = getattr(page, "bring_to_front", None)
        if callable(bring_to_front):
            bring_to_front()
        self._page = page
        self._start_screencast(page)
        self._context_engine.invalidate()

    def preview(self) -> Observation:
        """Capture a lightweight, replace-in-place frame for the Studio."""
        page = self._require_page()
        screenshot_path = ""
        if self._artifact_dir is not None:
            frame = page.screenshot(
                type="jpeg",
                quality=65,
                full_page=False,
            )
            if not isinstance(frame, bytes):
                raise TypeError("Browser preview did not return image bytes")
            screenshot_path = str(self._publish_preview_frame(frame))
        return Observation(
            url=str(getattr(page, "url", "")),
            title=self._safe_page_title(page),
            screenshot_path=screenshot_path,
            viewport=dict(self._viewport),
        )

    @staticmethod
    def action_specs() -> tuple[ActionSpec, ...]:
        """Return the action contract exposed to planners and registries."""
        definitions = (
            ("navigate", "Navigate to a URL", False, ("url",)),
            ("click", "Click a located element", True, ()),
            (
                "download",
                "Click a located element, wait for its download, and save the file",
                True,
                (),
            ),
            ("fill", "Replace text in a located field", True, ("text",)),
            ("press", "Press a key on a located element", True, ("key",)),
            ("select", "Select an option", True, ("value",)),
            ("check", "Set a checkbox or radio state", True, ()),
            ("hover", "Hover over a located element", True, ()),
            (
                "wait",
                "Wait for milliseconds (default 1000), or wait for a located "
                "element using state and the same duration as timeout",
                False,
                (),
            ),
            ("assert_text", "Assert located text", True, ("text",)),
            ("assert_visible", "Assert a located element is visible", True, ()),
            (
                "extract_value",
                "Read text, value, or an attribute from one located element",
                True,
                ("variable_name", "read"),
            ),
            ("screenshot", "Capture a page or element screenshot", False, ()),
            (
                "bind_page",
                "Bind the active tab to a stable page_ref",
                False,
                ("page_ref",),
            ),
            (
                "switch_page",
                "Switch by page_ref, URL/title traits, and optional opener_ref",
                False,
                ("page_ref",),
            ),
            (
                "close_page",
                "Close a tab identified by page_ref",
                False,
                ("page_ref",),
            ),
            (
                "open_popup",
                "Click an element and bind the popup it creates",
                True,
                ("page_ref",),
            ),
        )
        return tuple(
            ActionSpec(
                name=name,
                channel=ActionChannel.BROWSER,
                description=description,
                locator_required=locator_required,
                required_arguments=required_arguments,
                argument_schema=(
                    {
                        "milliseconds": "number",
                        "timeout": "number",
                        "timeout_ms": "number",
                        "time_ms": "number",
                        "state": "string",
                    }
                    if name == "wait"
                    else (
                        {
                            "page_ref": "string",
                            "url": "string",
                            "title": "string",
                            "opener_ref": "string",
                            "timeout": "number",
                        }
                        if name == "switch_page"
                        else (
                            {"page_ref": "string"}
                            if name in {"bind_page", "close_page"}
                            else (
                                {"page_ref": "string", "timeout": "number"}
                                if name == "open_popup"
                                else {}
                            )
                        )
                    )
                ),
            )
            for name, description, locator_required, required_arguments in definitions
        )

    def start(self) -> PlaywrightDevice:
        if self._page is not None:
            return self
        self._playwright = self._playwright_factory().start()
        browser_type = getattr(self._playwright, self._browser_name)
        self._browser = browser_type.launch(headless=self._headless)
        self._context = self._browser.new_context(viewport=dict(self._viewport))
        self._page = self._context.new_page()
        self._install_page_tracking(self._context)
        self._start_screencast(self._page)
        return self

    def close(self) -> None:
        if self._owns_browser and self._context is not None:
            self._context.close()
        if self._owns_browser and self._browser is not None:
            self._browser.close()
        if self._owns_browser and self._playwright is not None:
            self._playwright.stop()
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None
        self._page_ids.clear()
        self._pages_by_id.clear()
        self._next_page_number = 1
        self._page_listeners.clear()
        self._preview_listeners.clear()
        self._cdp_sessions.clear()
        self._page_refs.clear()
        self._preview_frame_paths.clear()
        self._preview_frame_number = 0
        self._context_engine.reset()

    def observe(self) -> Observation:
        self._last_observation = self.capture_context(
            ContextRequest(
                ContextPurpose.INSIGHT,
                force_refresh=True,
            )
        ).to_observation()
        return self._last_observation

    def capture_context(self, request: ContextRequest | None = None) -> UIContext:
        self._require_page()
        context = self._context_engine.capture(request)
        self._last_observation = context.to_observation()
        return context

    def freeze_context(self) -> UIContext:
        self._require_page()
        return self._context_engine.freeze()

    def unfreeze_context(self) -> None:
        self._context_engine.unfreeze()

    def _install_page_tracking(self, browser_context: Any) -> None:
        for page in list(getattr(browser_context, "pages", ())):
            self._register_page(page)
        on_page = getattr(browser_context, "on", None)
        if callable(on_page):
            on_page("page", self._activate_page)
        page_on = getattr(self._page, "on", None)
        if callable(page_on):
            page_on("popup", self._activate_page)
            current_page = self._page
            page_on(
                "close",
                lambda: self._recover_closed_page(current_page),
            )

    def _activate_page(self, page: Any) -> None:
        is_new = id(page) not in self._page_ids
        self._register_page(page)
        self._page = page
        page_on = getattr(page, "on", None)
        if is_new and callable(page_on):
            page_on(
                "close",
                lambda: self._recover_closed_page(page),
            )
        self._start_screencast(page)
        self._context_engine.invalidate()

    def _register_page(self, page: Any) -> str:
        key = id(page)
        existing = self._page_ids.get(key)
        if existing is not None:
            return existing
        page_id = f"page_{self._next_page_number}"
        self._next_page_number += 1
        self._page_ids[key] = page_id
        self._pages_by_id[page_id] = page
        for listener in self._page_listeners:
            listener(page)
        return page_id

    def _opener_id(self, page: Any) -> str:
        opener = getattr(page, "opener", None)
        if not callable(opener):
            return ""
        try:
            parent = opener()
        except Exception:
            return ""
        return self._register_page(parent) if parent is not None else ""

    @staticmethod
    def _safe_page_title(page: Any) -> str:
        title = getattr(page, "title", None)
        if not callable(title):
            return ""
        try:
            return str(title())
        except Exception:
            return ""

    def _recover_closed_page(self, closed_page: Any) -> None:
        page_id = self._page_ids.pop(id(closed_page), None)
        if page_id is not None:
            self._pages_by_id.pop(page_id, None)
        self._cdp_sessions.pop(id(closed_page), None)
        self._page_refs = {
            reference: page
            for reference, page in self._page_refs.items()
            if page is not closed_page
        }
        if self._page is not closed_page:
            return
        pages = list(getattr(self._context, "pages", ()))
        candidates = [
            page for page in pages if page is not closed_page and not self._page_is_closed(page)
        ]
        self._page = candidates[-1] if candidates else None
        self._context_engine.invalidate()

    def _find_page(self, arguments: dict[str, JsonValue]) -> Any:
        page_ref = arguments.get("page_ref")
        if isinstance(page_ref, str) and page_ref:
            page = self._page_refs.get(page_ref)
            if page is not None and not self._page_is_closed(page):
                return page
            raise ValueError(f"Browser page reference is not available: {page_ref}")
        url = str(arguments.get("url", ""))
        title = str(arguments.get("title", ""))
        opener_ref = str(arguments.get("opener_ref", ""))
        opener = self._page_refs.get(opener_ref) if opener_ref else None
        matches: list[Any] = []
        for page in getattr(self._context, "pages", ()):
            if self._page_is_closed(page):
                continue
            if url and url not in str(getattr(page, "url", "")):
                continue
            if title and title not in self._safe_page_title(page):
                continue
            page_opener = getattr(page, "opener", None)
            if opener is not None and (not callable(page_opener) or page_opener() is not opener):
                continue
            matches.append(page)
        if len(matches) != 1:
            raise ValueError(f"Page selector matched {len(matches)} tabs; expected exactly one")
        return matches[0]

    def _start_screencast(self, page: Any) -> None:
        """Start Chromium's CDP frame stream, falling back silently elsewhere."""
        if not self._preview_listeners or id(page) in self._cdp_sessions:
            return
        new_session = getattr(self._context, "new_cdp_session", None)
        if not callable(new_session):
            return
        try:
            session = new_session(page)
            page_id = self._register_page(page)

            def on_frame(event: dict[str, Any]) -> None:
                session.send(
                    "Page.screencastFrameAck",
                    {"sessionId": event["sessionId"]},
                )
                self._write_stream_frame(page, page_id, str(event["data"]))

            session.on("Page.screencastFrame", on_frame)
            session.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 65,
                    "maxWidth": self._viewport["width"],
                    "maxHeight": self._viewport["height"],
                    "everyNthFrame": 1,
                },
            )
            self._cdp_sessions[id(page)] = session
        except Exception:
            return

    def _write_stream_frame(self, page: Any, page_id: str, encoded: str) -> None:
        if self._artifact_dir is None or page is not self._page:
            return
        target = self._publish_preview_frame(base64.b64decode(encoded))
        observation = Observation(
            url=str(getattr(page, "url", "")),
            title=self._safe_page_title(page),
            screenshot_path=str(target),
            viewport=dict(self._viewport),
        )
        for listener in tuple(self._preview_listeners):
            listener(observation, page_id)

    def _publish_preview_frame(self, frame: bytes) -> Path:
        """Publish an immutable frame without replacing a WebView-locked file."""
        if self._artifact_dir is None:
            raise RuntimeError("Preview frames require an artifact directory")
        self._artifact_dir.mkdir(parents=True, exist_ok=True)
        with self._preview_frame_lock:
            self._preview_frame_number += 1
            target = self._artifact_dir / (f"live-preview-{self._preview_frame_number:08d}.jpg")
            target.write_bytes(frame)
            self._preview_frame_paths.append(target)
            while len(self._preview_frame_paths) > 8:
                expired = self._preview_frame_paths.popleft()
                # Windows may keep a frame locked while WebView decodes it.
                with suppress(PermissionError):
                    expired.unlink(missing_ok=True)
            return target

    @staticmethod
    def _page_is_closed(page: Any) -> bool:
        is_closed = getattr(page, "is_closed", None)
        if callable(is_closed):
            return bool(is_closed())
        return False

    def execute(
        self,
        action: Action,
        control: ActionExecutionContext | None = None,
    ) -> ActionResult:
        if action.channel is not ActionChannel.BROWSER:
            return ActionResult(
                status=ActionStatus.BLOCKED,
                error="PlaywrightDevice only executes browser actions",
            )
        try:
            if control is not None:
                control.raise_if_stopped()
            self._reset_locator_resolution()
            output = self._execute_action(action, control)
            if control is not None:
                control.raise_if_stopped()
            if self._last_locator_resolution is not None:
                output["locator_resolution"] = self._last_locator_resolution.to_dict()
            return ActionResult.succeeded(output)
        except Exception as exc:
            if isinstance(exc, ActionCancelledError):
                return (
                    control.cancelled_result()
                    if control is not None
                    else ActionResult(
                        ActionStatus.CANCELLED,
                        error=str(exc),
                        error_category="cancelled",
                    )
                )
            if self._is_timeout(exc):
                return ActionResult(
                    status=ActionStatus.TIMEOUT,
                    error=str(exc),
                )
            return ActionResult.failed(str(exc))
        finally:
            self._context_engine.invalidate()

    def _reset_locator_resolution(self) -> None:
        self._last_locator_resolution = None

    def _execute_action(
        self,
        action: Action,
        control: ActionExecutionContext | None = None,
    ) -> dict[str, JsonValue]:
        page = self._require_page()
        arguments = dict(action.arguments)
        if control is not None and action.name != "wait":
            remaining = control.remaining_seconds()
            if remaining is not None:
                remaining_ms = max(1.0, remaining * 1000)
                requested = arguments.get("timeout")
                if isinstance(requested, (int, float)) and not isinstance(requested, bool):
                    remaining_ms = min(float(requested), remaining_ms)
                arguments["timeout"] = remaining_ms
        if action.name == "navigate":
            url = self._required_text(arguments, "url")
            page.goto(
                url,
                **self._options(arguments, "wait_until", "timeout", "referer"),
            )
            return {"action": action.name, "url": url}

        if action.name == "wait":
            milliseconds = resolve_wait_milliseconds(action)
            if control is not None:
                remaining = control.remaining_seconds()
                if remaining is not None:
                    milliseconds = min(milliseconds, remaining * 1000)
            if action.locator is None:
                if control is None:
                    page.wait_for_timeout(float(milliseconds))
                else:
                    remaining = float(milliseconds)
                    while remaining > 0:
                        control.raise_if_stopped()
                        interval = min(remaining, 100.0)
                        page.wait_for_timeout(interval)
                        remaining -= interval
            else:
                self._resolve_action_target(action).primary.wait_for(
                    state=str(arguments.get("state", "visible")),
                    timeout=float(milliseconds),
                )
            return {"action": action.name, "milliseconds": milliseconds}

        if action.name == "bind_page":
            page_ref = self._required_text(arguments, "page_ref")
            self._page_refs[page_ref] = page
            return {"action": action.name, "page_ref": page_ref}

        if action.name == "switch_page":
            selected = self._find_page(arguments)
            selected_id = self._register_page(selected)
            self.activate_page(selected_id)
            return {"action": action.name, "page_id": selected_id}

        if action.name == "close_page":
            page_ref = self._required_text(arguments, "page_ref")
            selected = self._page_refs.get(page_ref)
            if selected is None or self._page_is_closed(selected):
                raise ValueError(f"Browser page reference is not available: {page_ref}")
            selected.close()
            self._recover_closed_page(selected)
            return {"action": action.name, "page_ref": page_ref}

        if action.name == "open_popup":
            page_ref = self._required_text(arguments, "page_ref")
            timeout = arguments.get("timeout", 30_000)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ValueError("open_popup timeout must be numeric")
            target = self._resolve_action_target(action).primary
            with page.expect_popup(timeout=float(timeout)) as popup_info:
                target.click(timeout=float(timeout))
            popup = popup_info.value
            self._page_refs[page_ref] = popup
            self._activate_page(popup)
            return {"action": action.name, "page_ref": page_ref}

        if action.name == "screenshot":
            options = self._options(
                arguments,
                "path",
                "full_page",
                "animations",
                "caret",
                "omit_background",
                "quality",
                "type",
                "timeout",
            )
            if action.locator is None:
                page.screenshot(**options)
            else:
                options.pop("full_page", None)
                self._resolve_action_target(action).primary.screenshot(**options)
            path = arguments.get("path", "")
            return {
                "action": action.name,
                "path": str(path) if path is not None else "",
            }

        interaction = self._resolve_action_target(
            action,
            require_actionable=(
                action.name in {"click", "download", "hover"}
                and action.arguments.get("force") is not True
            ),
        )
        target = interaction.primary
        if action.name == "extract_value":
            read = str(arguments.get("read", "text"))
            if read == "text":
                value = target.inner_text()
            elif read == "value":
                value = target.input_value()
            elif read == "attribute":
                attribute = self._required_text(arguments, "attribute")
                value = target.get_attribute(attribute)
                if value is None:
                    raise ValueError(f"Located element has no {attribute!r} attribute")
            else:
                raise ValueError(f"Unsupported extraction read mode: {read}")
            return {
                "action": action.name,
                "variable_name": self._required_text(arguments, "variable_name"),
                "value": str(value),
            }
        if action.name == "click":
            self._click_with_fallback(
                interaction,
                self._options(
                    arguments,
                    "button", "click_count", "delay", "force", "modifiers",
                    "no_wait_after", "timeout", "trial",
                ),
                control,
            )
        elif action.name == "download":
            timeout = arguments.get("timeout", 30_000)
            if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
                raise ValueError("download timeout must be numeric")
            with page.expect_download(timeout=float(timeout)) as download_info:
                if control is None:
                    target.click()
                else:
                    target.click(timeout=float(timeout))
            download = download_info.value
            requested_path = arguments.get("path")
            if requested_path is not None and not isinstance(requested_path, str):
                raise ValueError("download path must be a string")
            download_root = (
                self._artifact_dir.parent / "downloads"
                if self._artifact_dir is not None
                else Path.cwd() / "downloads"
            )
            destination = (
                Path(requested_path)
                if requested_path
                else download_root / Path(download.suggested_filename).name
            )
            if not destination.is_absolute():
                destination = download_root / destination
            destination = destination.resolve()
            destination.parent.mkdir(parents=True, exist_ok=True)
            download.save_as(str(destination))
            return {
                "action": action.name,
                "filename": destination.name,
                "path": str(destination),
                "url": str(download.url),
            }
        elif action.name == "fill":
            text = self._required_text(arguments, "text", fallback="value")
            target.fill(
                text,
                **self._options(arguments, "force", "no_wait_after", "timeout"),
            )
        elif action.name == "press":
            key = self._required_text(arguments, "key")
            target.press(
                key,
                **self._options(arguments, "delay", "no_wait_after", "timeout"),
            )
        elif action.name == "select":
            option: dict[str, Any]
            if "values" in arguments:
                option = {"value": arguments["values"]}
            elif "label" in arguments:
                option = {"label": arguments["label"]}
            elif "index" in arguments:
                option = {"index": arguments["index"]}
            else:
                option = {"value": self._required_text(arguments, "value")}
            target.select_option(
                **option,
                **self._options(arguments, "force", "no_wait_after", "timeout"),
            )
        elif action.name == "check":
            method = target.check if bool(arguments.get("checked", True)) else target.uncheck
            method(
                **self._options(
                    arguments,
                    "force",
                    "no_wait_after",
                    "position",
                    "timeout",
                    "trial",
                )
            )
        elif action.name == "hover":
            target.hover(
                **self._options(
                    arguments,
                    "force",
                    "modifiers",
                    "position",
                    "timeout",
                    "trial",
                )
            )
        elif action.name == "assert_text":
            expected = self._required_text(arguments, "text", fallback="expected")
            actual = str(target.inner_text(**self._options(arguments, "timeout")))
            exact = bool(arguments.get("exact", False))
            matches = actual == expected if exact else expected in actual
            if not matches:
                comparison = "equal" if exact else "contain"
                raise AssertionError(f"Expected text to {comparison} {expected!r}; got {actual!r}")
            return {"action": action.name, "actual_text": actual}
        elif action.name == "assert_visible":
            if not bool(target.is_visible()):
                raise AssertionError("Expected located element to be visible")
        else:
            raise ValueError(f"Unsupported browser action: {action.name}")
        return {"action": action.name}

    def _resolve_locator(
        self,
        locator: Locator | None,
        *,
        require_actionable: bool = False,
    ) -> Any:
        if locator is None:
            raise ValueError("This browser action requires a locator")
        return self._resolve_target(
            ElementTarget(candidates=(locator,)),
            require_actionable=require_actionable,
        ).primary

    def _resolve_action_target(
        self,
        action: Action,
        *,
        require_actionable: bool = False,
    ) -> InteractionResolution:
        target = action.target
        if target is None:
            raise ValueError("This browser action requires an element target")
        return self._resolve_target(target, require_actionable=require_actionable)

    def _resolve_target(
        self,
        target: ElementTarget,
        *,
        require_actionable: bool = False,
    ) -> InteractionResolution:
        resolution = self._interaction_engine.resolve(
            target,
            self._last_observation,
            require_actionable=require_actionable,
        )
        self._last_locator_resolution = resolution.locator_resolution
        return resolution

    def _click_with_fallback(
        self,
        interaction: InteractionResolution,
        options: dict[str, JsonValue],
        control: ActionExecutionContext | None,
    ) -> None:
        failures: list[str] = []
        last_error: Exception | None = None
        for candidate in interaction.candidates:
            if control is not None:
                control.raise_if_stopped()
                remaining = control.remaining_seconds()
                if remaining is not None:
                    options["timeout"] = max(1.0, remaining * 1000)
            try:
                candidate.handle.click(**options)
                if candidate.locator != interaction.locator_resolution.selected:
                    self._last_locator_resolution = LocatorResolution(
                        candidate.locator,
                        interaction.locator_resolution.candidates,
                    )
                return
            except Exception as exc:
                last_error = exc
                failures.append(f"{candidate.locator.strategy}: {exc}")
        if last_error is None:
            raise RuntimeError("No viable locator candidate was executed")
        if len(failures) > 1:
            last_error.add_note("Previous locator failures: " + " | ".join(failures[:-1]))
        raise last_error

    def _build_locator(self, locator: Locator, *, scope: Any | None = None) -> Any:
        page = scope or self._require_page()
        if locator.strategy == "role":
            options: dict[str, Any] = {}
            if locator.name is not None:
                options = {"name": locator.name, "exact": locator.exact}
            return page.get_by_role(locator.value, **options)
        if locator.strategy == "test_id":
            return page.get_by_test_id(locator.value)
        if locator.strategy == "label":
            return page.get_by_label(locator.value, exact=locator.exact)
        if locator.strategy == "placeholder":
            return page.get_by_placeholder(locator.value, exact=locator.exact)
        if locator.strategy == "text":
            return page.get_by_text(locator.value, exact=locator.exact)
        if locator.strategy == "css":
            return page.locator(locator.value)
        if locator.strategy == "xpath":
            selector = locator.value
            if not selector.startswith("xpath="):
                selector = f"xpath={selector}"
            return page.locator(selector)
        if locator.strategy in {"fallback", "selector"}:
            return page.locator(locator.value)
        raise ValueError(f"Unsupported locator strategy: {locator.strategy}")

    @classmethod
    def _locator_candidates(cls, locator: Locator) -> tuple[Locator, ...]:
        candidates = [locator]
        for fallback in locator.fallbacks:
            candidates.extend(cls._locator_candidates(fallback))
        return tuple(candidates)

    def _require_page(self) -> Any:
        if self._page is None:
            raise RuntimeError("PlaywrightDevice is not started")
        return self._page

    @staticmethod
    def _required_text(
        arguments: dict[str, JsonValue],
        key: str,
        *,
        fallback: str | None = None,
    ) -> str:
        value = arguments.get(key)
        if value is None and fallback is not None:
            value = arguments.get(fallback)
        if value is None:
            raise ValueError(f"Browser action requires argument: {key}")
        return str(value)

    @staticmethod
    def _options(arguments: dict[str, JsonValue], *names: str) -> dict[str, JsonValue]:
        return {
            name: arguments[name]
            for name in names
            if name in arguments and arguments[name] is not None
        }

    @staticmethod
    def _is_timeout(error: Exception) -> bool:
        if isinstance(error, TimeoutError):
            return True
        try:
            from playwright.sync_api import TimeoutError as PlaywrightTimeout
        except ImportError:
            return False
        return isinstance(error, PlaywrightTimeout)


__all__ = ["PlaywrightDevice"]
