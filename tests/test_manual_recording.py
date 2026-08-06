from pathlib import Path

import pytest

from web_e2e_test_studio.browser import PlaywrightDevice
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionStatus,
    Locator,
)
from web_e2e_test_studio.recording.manual import (
    BrowserEventRecorder,
    ManualEventCompiler,
)


def test_manual_click_prefers_accessible_locator_with_ordered_fallbacks() -> None:
    actions = ManualEventCompiler().compile(
        [
            {
                "type": "click",
                "target": {
                    "role": "button",
                    "name": "Sign in",
                    "test_id": "sign-in",
                    "label": "Submit login",
                    "text": "Sign in",
                    "css": "#login button",
                    "xpath": "//*[@id='login']/button",
                    "nth": 1,
                },
            }
        ]
    )

    assert len(actions) == 1
    action = actions[0]
    assert action.name == "click"
    assert action.channel is ActionChannel.BROWSER
    assert action.locator is not None
    assert (
        action.locator.strategy,
        action.locator.value,
        action.locator.name,
        action.locator.nth,
    ) == ("role", "button", "Sign in", 1)
    assert [item.strategy for item in action.locator.fallbacks] == [
        "test_id",
        "label",
        "text",
        "css",
        "xpath",
    ]


def test_manual_compiler_coalesces_consecutive_input_for_the_same_field() -> None:
    actions = ManualEventCompiler().compile(
        [
            {
                "type": "input",
                "value": "a",
                "target": {"label": "Search", "css": "#search"},
            },
            {
                "type": "input",
                "value": "annual",
                "target": {"label": "Search", "css": "#search"},
            },
            {
                "type": "click",
                "target": {"role": "button", "name": "Search"},
            },
        ]
    )

    assert [action.name for action in actions] == ["fill", "click"]
    assert actions[0].arguments == {"text": "annual"}


def test_manual_compiler_replaces_password_values_with_environment_references() -> None:
    compiler = ManualEventCompiler()

    first = compiler.compile(
        [
            {
                "type": "input",
                "value": "do-not-store-this",
                "target": {
                    "label": "Password",
                    "css": "#password",
                    "input_type": "password",
                },
            }
        ]
    )
    second = compiler.compile(
        [
            {
                "type": "input",
                "value": "also-secret",
                "target": {
                    "label": "One-time password",
                    "css": "#otp",
                    "input_type": "password",
                },
            }
        ]
    )

    assert first[0].arguments == {"text": "${ENV:RECORDED_PASSWORD_1}"}
    assert second[0].arguments == {"text": "${ENV:RECORDED_PASSWORD_2}"}
    assert "do-not-store-this" not in repr(first)
    assert "also-secret" not in repr(second)


def test_manual_compiler_maps_navigation_and_change_events() -> None:
    actions = ManualEventCompiler().compile(
        [
            {"type": "navigation", "url": "https://example.test/orders"},
            {
                "type": "change",
                "value": "paid",
                "target": {"tag": "select", "label": "Status"},
            },
            {
                "type": "change",
                "checked": True,
                "target": {
                    "tag": "input",
                    "input_type": "checkbox",
                    "label": "Include archived",
                },
            },
        ]
    )

    assert [action.name for action in actions] == [
        "navigate",
        "select",
        "check",
    ]
    assert actions[0].arguments == {"url": "https://example.test/orders"}
    assert actions[1].arguments == {"value": "paid"}
    assert actions[2].arguments == {"checked": True}


class EventQueuePage:
    def __init__(self, events) -> None:  # noqa: ANN001
        self.events = list(events)
        self.evaluated: list[str] = []

    def evaluate(self, script: str):  # noqa: ANN201
        self.evaluated.append(script)
        drained = list(self.events)
        self.events.clear()
        return drained


def test_browser_event_recorder_installs_and_atomically_drains_page_queue() -> None:
    page = EventQueuePage(
        [
            {
                "type": "click",
                "target": {"role": "button", "name": "Save"},
            }
        ]
    )
    recorder = BrowserEventRecorder()

    installation = recorder.installation_script()
    actions = recorder.drain(page)

    assert "addEventListener('click'" in installation
    assert "addEventListener('input'" in installation
    assert "addEventListener('change'" in installation
    assert "pushState" in installation
    assert [action.name for action in actions] == ["click"]
    assert "splice" in page.evaluated[0]
    assert recorder.drain(page) == []


class FakeConsoleMessage:
    def __init__(self, text: str) -> None:
        self.text = text


class FakeCrossNavigationPage:
    def __init__(self) -> None:
        self.console_handler = None
        self.init_scripts: list[str] = []

    def on(self, event: str, handler) -> None:  # noqa: ANN001
        assert event == "console"
        self.console_handler = handler

    def add_init_script(self, *, script: str) -> None:
        self.init_scripts.append(script)

    def evaluate(self, script: str):  # noqa: ANN201
        if "splice(0, queue.length)" in script:
            return []
        return None

    def wait_for_timeout(self, milliseconds: int) -> None:
        assert milliseconds == 0


def test_browser_event_recorder_keeps_console_event_across_navigation() -> None:
    page = FakeCrossNavigationPage()
    recorder = BrowserEventRecorder()
    recorder.install(page)
    assert page.console_handler is not None

    page.console_handler(
        FakeConsoleMessage(
            '__WEB_E2E_TEST_STUDIO_EVENT__{"type":"click","timestamp":10,'
            '"target":{"role":"button","name":"Continue"}}'
        )
    )
    actions = recorder.drain(page)

    assert [action.name for action in actions] == ["click"]
    assert page.init_scripts
    assert "__WEB_E2E_TEST_STUDIO_EVENT__" in page.init_scripts[0]


def test_browser_event_recorder_promotes_manual_popup_to_cross_tab_actions() -> None:
    class _Page(FakeCrossNavigationPage):
        def __init__(self, opener=None) -> None:  # noqa: ANN001
            super().__init__()
            self._opener = opener

        def opener(self):  # noqa: ANN201
            return self._opener

    main = _Page()
    popup = _Page(main)
    recorder = BrowserEventRecorder()
    recorder.install(main)
    assert main.console_handler is not None
    main.console_handler(
        FakeConsoleMessage(
            '__WEB_E2E_TEST_STUDIO_EVENT__{"type":"click","timestamp":10,'
            '"target":{"role":"link","name":"Open report"}}'
        )
    )
    recorder.install(popup)
    assert popup.console_handler is not None
    popup.console_handler(
        FakeConsoleMessage(
            '__WEB_E2E_TEST_STUDIO_EVENT__{"type":"input","timestamp":19,'
            '"value":"q","target":{"label":"Search",'
            '"tag":"input","input_type":"text"}}'
        )
    )
    popup.console_handler(
        FakeConsoleMessage(
            '__WEB_E2E_TEST_STUDIO_EVENT__{"type":"input","timestamp":20,'
            '"value":"quarterly","target":{"label":"Search",'
            '"tag":"input","input_type":"text"}}'
        )
    )

    actions = recorder.drain_all()

    assert [action.name for action in actions] == [
        "bind_page",
        "open_popup",
        "fill",
    ]
    assert actions[0].arguments == {"page_ref": "main"}
    assert actions[1].arguments == {"page_ref": "popup_1"}
    assert actions[1].locator == Locator("role", "link", name="Open report")
    assert actions[2].arguments == {"text": "quarterly"}

    main.console_handler(
        FakeConsoleMessage(
            '__WEB_E2E_TEST_STUDIO_EVENT__{"type":"click","timestamp":30,'
            '"target":{"role":"button","name":"Refresh"}}'
        )
    )
    follow_up = recorder.drain_all()

    assert [action.name for action in follow_up] == ["switch_page", "click"]
    assert follow_up[0].arguments == {"page_ref": "main"}


class FakeLifecycleContext:
    def __init__(self, page) -> None:  # noqa: ANN001
        self.page = page
        self.closed = False

    def new_page(self):  # noqa: ANN201
        return self.page

    def close(self) -> None:
        self.closed = True


class FakeLifecycleBrowser:
    def __init__(self, context: FakeLifecycleContext) -> None:
        self.context = context
        self.closed = False
        self.context_options = None

    def new_context(self, **options):  # noqa: ANN003, ANN201
        self.context_options = options
        return self.context

    def close(self) -> None:
        self.closed = True


class FakeBrowserType:
    def __init__(self, browser: FakeLifecycleBrowser) -> None:
        self.browser = browser
        self.launch_options = None

    def launch(self, **options):  # noqa: ANN003, ANN201
        self.launch_options = options
        return self.browser


class FakePlaywright:
    def __init__(self, browser: FakeLifecycleBrowser) -> None:
        self.chromium = FakeBrowserType(browser)
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class FakePlaywrightStarter:
    def __init__(self, playwright: FakePlaywright) -> None:
        self.playwright = playwright

    def start(self) -> FakePlaywright:
        return self.playwright


def test_playwright_device_owns_browser_lifecycle() -> None:
    page = object()
    context = FakeLifecycleContext(page)
    browser = FakeLifecycleBrowser(context)
    playwright = FakePlaywright(browser)
    device = PlaywrightDevice(
        headless=False,
        viewport={"width": 1024, "height": 768},
        playwright_factory=lambda: FakePlaywrightStarter(playwright),
    )

    started = device.start()
    device.close()

    assert started is device
    assert device.page is None
    assert playwright.chromium.launch_options == {"headless": False}
    assert browser.context_options == {"viewport": {"width": 1024, "height": 768}}
    assert context.closed is True
    assert browser.closed is True
    assert playwright.stopped is True


class FakeObservationPage:
    url = "https://example.test/dashboard"

    def title(self) -> str:
        return "Dashboard"

    def evaluate(self, script: str):  # noqa: ANN201
        assert "interactive_elements" in script
        return {
            "viewport": {"width": 1280, "height": 720},
            "interactive_elements": [{"tag": "button", "text": "Create report"}],
            "page_text": "Reports Create report",
        }


def test_playwright_device_observes_serializable_page_state() -> None:
    device = PlaywrightDevice(page=FakeObservationPage())

    observation = device.observe()

    assert observation.url == "https://example.test/dashboard"
    assert observation.title == "Dashboard"
    assert observation.viewport == {"width": 1280, "height": 720}
    assert observation.interactive_elements == ({"tag": "button", "text": "Create report"},)
    assert observation.page_text == "Reports Create report"


class NavigatingObservationPage(FakeObservationPage):
    def __init__(self) -> None:
        self.evaluate_calls = 0
        self.load_state_waits: list[tuple[str, int]] = []

    def evaluate(self, script: str):  # noqa: ANN201
        self.evaluate_calls += 1
        if self.evaluate_calls == 1:
            raise RuntimeError(
                "Page.evaluate: Execution context was destroyed, "
                "most likely because of a navigation"
            )
        return super().evaluate(script)

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        self.load_state_waits.append((state, timeout))


def test_playwright_device_retries_observation_after_navigation() -> None:
    page = NavigatingObservationPage()
    device = PlaywrightDevice(page=page)

    observation = device.observe()

    assert observation.title == "Dashboard"
    assert page.evaluate_calls == 2
    assert page.load_state_waits == [("domcontentloaded", 5_000)]


class ScreenshotObservationPage(FakeObservationPage):
    def screenshot(self, *, path: str, full_page: bool) -> None:
        assert full_page is False
        Path(path).write_bytes(b"png")


def test_playwright_device_observation_can_capture_vlm_screenshot(
    tmp_path: Path,
) -> None:
    device = PlaywrightDevice(
        page=ScreenshotObservationPage(),
        artifact_dir=tmp_path / "observations",
    )

    observation = device.observe()

    screenshot = Path(observation.screenshot_path)
    assert screenshot.is_file()
    assert screenshot.read_bytes() == b"png"


class FakeActionLocator:
    def __init__(
        self,
        identity: str,
        calls: list[tuple],
        *,
        count: int = 1,
    ) -> None:
        self.identity = identity
        self.calls = calls
        self._count = count

    def count(self) -> int:
        return self._count

    def nth(self, index: int) -> "FakeActionLocator":
        self.calls.append(("nth", self.identity, index))
        return FakeActionLocator(f"{self.identity}.nth({index})", self.calls, count=1)

    def click(self) -> None:
        self.calls.append(("click", self.identity))


class FakeLocatorPage:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def get_by_role(self, role: str, **options) -> FakeActionLocator:  # noqa: ANN003
        self.calls.append(("role", role, options))
        return FakeActionLocator("role", self.calls, count=0)

    def get_by_test_id(self, value: str) -> FakeActionLocator:
        self.calls.append(("test_id", value))
        return FakeActionLocator("test_id", self.calls, count=3)


def test_playwright_device_uses_locator_fallback_and_nth() -> None:
    page = FakeLocatorPage()
    action = Action(
        name="click",
        channel=ActionChannel.BROWSER,
        locator=Locator(
            "role",
            "button",
            name="Save",
            fallbacks=(Locator("test_id", "save", nth=2),),
        ),
    )

    result = PlaywrightDevice(page=page).execute(action)

    assert result.status is ActionStatus.SUCCESS
    resolution = result.output["locator_resolution"]
    assert isinstance(resolution, dict)
    assert resolution["selected"]["strategy"] == "test_id"
    assert resolution["selected"]["nth"] == 2
    assert page.calls == [
        ("role", "button", {"name": "Save", "exact": True}),
        ("test_id", "save"),
        ("nth", "test_id", 2),
        ("click", "test_id.nth(2)"),
    ]


class FakeExecutionLocator:
    def __init__(self, performed: list[tuple]) -> None:
        self.performed = performed

    def count(self) -> int:
        return 1

    def click(self, **options) -> None:  # noqa: ANN003
        self.performed.append(("click", options))

    def fill(self, text: str, **options) -> None:  # noqa: ANN003
        self.performed.append(("fill", text, options))

    def press(self, key: str, **options) -> None:  # noqa: ANN003
        self.performed.append(("press", key, options))

    def select_option(self, **options):  # noqa: ANN003, ANN201
        self.performed.append(("select", options))
        return [str(next(iter(options.values())))]

    def check(self, **options) -> None:  # noqa: ANN003
        self.performed.append(("check", options))

    def uncheck(self, **options) -> None:  # noqa: ANN003
        self.performed.append(("uncheck", options))

    def hover(self, **options) -> None:  # noqa: ANN003
        self.performed.append(("hover", options))

    def inner_text(self) -> str:
        return "Ready to ship"

    def is_visible(self) -> bool:
        return True

    def screenshot(self, **options):  # noqa: ANN003, ANN201
        self.performed.append(("element_screenshot", options))
        return b"png"


class FakeExecutionPage:
    def __init__(self) -> None:
        self.performed: list[tuple] = []

    def locator(self, selector: str) -> FakeExecutionLocator:
        self.performed.append(("locator", selector))
        return FakeExecutionLocator(self.performed)

    def goto(self, url: str, **options) -> None:  # noqa: ANN003
        self.performed.append(("navigate", url, options))

    def wait_for_timeout(self, milliseconds: float) -> None:
        self.performed.append(("wait", milliseconds))

    def screenshot(self, **options):  # noqa: ANN003, ANN201
        self.performed.append(("screenshot", options))
        return b"png"


def test_playwright_device_executes_supported_deterministic_actions() -> None:
    page = FakeExecutionPage()
    device = PlaywrightDevice(page=page)
    target = Locator("css", "#target")
    actions = [
        Action(
            "navigate",
            ActionChannel.BROWSER,
            {"url": "https://example.test"},
        ),
        Action("click", ActionChannel.BROWSER, locator=target),
        Action(
            "fill",
            ActionChannel.BROWSER,
            {"text": "invoice"},
            target,
        ),
        Action(
            "press",
            ActionChannel.BROWSER,
            {"key": "Enter"},
            target,
        ),
        Action(
            "select",
            ActionChannel.BROWSER,
            {"value": "paid"},
            target,
        ),
        Action(
            "check",
            ActionChannel.BROWSER,
            {"checked": True},
            target,
        ),
        Action("hover", ActionChannel.BROWSER, locator=target),
        Action(
            "wait",
            ActionChannel.BROWSER,
            {"milliseconds": 250},
        ),
        Action(
            "assert_text",
            ActionChannel.BROWSER,
            {"text": "Ready"},
            target,
        ),
        Action("assert_visible", ActionChannel.BROWSER, locator=target),
        Action(
            "screenshot",
            ActionChannel.BROWSER,
            {"path": "artifacts/final.png", "full_page": True},
        ),
    ]

    results = [device.execute(action) for action in actions]

    assert all(result.status is ActionStatus.SUCCESS for result in results)
    assert [item[0] for item in page.performed if item[0] != "locator"] == [
        "navigate",
        "click",
        "fill",
        "press",
        "select",
        "check",
        "hover",
        "wait",
        "screenshot",
    ]


def test_playwright_device_extracts_one_located_page_value() -> None:
    page = FakeExecutionPage()
    device = PlaywrightDevice(page=page)

    result = device.execute(
        Action(
            "extract_value",
            ActionChannel.BROWSER,
            {"variable_name": "status", "read": "text"},
            Locator("css", "#status"),
        )
    )

    assert result.output["variable_name"] == "status"
    assert result.output["value"] == "Ready to ship"


def test_playwright_device_tracks_and_switches_multiple_pages() -> None:
    class _TrackedContext:
        def __init__(self) -> None:
            self.pages: list[_TrackedPage] = []
            self.listeners = {}

        def on(self, event, callback) -> None:  # noqa: ANN001
            self.listeners[event] = callback

    class _TrackedPage:
        def __init__(
            self,
            context: _TrackedContext,
            url: str,
            title: str,
        ) -> None:
            self.context = context
            self.url = url
            self._title = title
            self.listeners = {}
            self.front_count = 0
            self.closed = False

        def on(self, event, callback) -> None:  # noqa: ANN001
            self.listeners[event] = callback

        def title(self) -> str:
            return self._title

        def bring_to_front(self) -> None:
            self.front_count += 1

        def is_closed(self) -> bool:
            return self.closed

    context = _TrackedContext()
    first = _TrackedPage(context, "https://example.test/one", "One")
    context.pages.append(first)
    device = PlaywrightDevice(page=first)
    installed: list[_TrackedPage] = []
    device.add_page_listener(installed.append)

    second = _TrackedPage(context, "https://example.test/two", "Two")
    context.pages.append(second)
    context.listeners["page"](second)

    pages = device.pages()
    assert len(pages) == 2
    assert pages[0].active is False
    assert pages[1].active is True
    assert pages[1].title == "Two"
    assert installed == [first, second]

    device.activate_page(pages[0].id)

    assert device.page is first
    assert first.front_count == 1
    assert device.pages()[0].active is True

    second.closed = True
    context.pages.remove(second)
    second.listeners["close"]()
    assert len(device.pages()) == 1
    with pytest.raises(ValueError, match="not available"):
        device.activate_page(pages[1].id)


def test_playwright_device_publishes_immutable_lightweight_preview_files(
    tmp_path: Path,
) -> None:
    class _PreviewPage:
        context = None
        url = "https://example.test/live"

        def on(self, event, callback) -> None:  # noqa: ANN001
            return None

        def title(self) -> str:
            return "Live"

        def screenshot(self, **options) -> bytes:  # noqa: ANN003
            return b"jpeg-frame"

    device = PlaywrightDevice(
        page=_PreviewPage(),
        artifact_dir=tmp_path,
    )

    first = device.preview()
    second = device.preview()

    assert first.screenshot_path == str(tmp_path / "live-preview-00000001.jpg")
    assert second.screenshot_path == str(tmp_path / "live-preview-00000002.jpg")
    assert second.screenshot_path != first.screenshot_path
    assert Path(second.screenshot_path).read_bytes() == b"jpeg-frame"
    assert second.url == "https://example.test/live"


def test_playwright_preview_does_not_replace_a_locked_frame(tmp_path: Path, monkeypatch) -> None:  # noqa: ANN001
    class _PreviewPage:
        context = None
        url = "https://example.test/live"

        def on(self, event, callback) -> None:  # noqa: ANN001
            return None

        def title(self) -> str:
            return "Live"

        def screenshot(self, **options) -> bytes:  # noqa: ANN003
            return b"new-frame"

    def locked_replace(self, target):  # noqa: ANN001, ANN202
        raise PermissionError(5, "Access denied", str(target))

    monkeypatch.setattr(Path, "replace", locked_replace)
    device = PlaywrightDevice(page=_PreviewPage(), artifact_dir=tmp_path)

    observation = device.preview()

    assert Path(observation.screenshot_path).read_bytes() == b"new-frame"


def test_playwright_device_publishes_cdp_screencast_frames(
    tmp_path: Path,
) -> None:
    import base64

    class _Session:
        def __init__(self) -> None:
            self.listeners = {}
            self.sent = []

        def on(self, event, callback) -> None:  # noqa: ANN001
            self.listeners[event] = callback

        def send(self, method, params=None) -> None:  # noqa: ANN001
            self.sent.append((method, params))

    class _Context:
        def __init__(self) -> None:
            self.pages = []
            self.session = _Session()

        def on(self, event, callback) -> None:  # noqa: ANN001
            return None

        def new_cdp_session(self, page):  # noqa: ANN001
            return self.session

    class _Page:
        url = "https://example.test/live"

        def __init__(self, context) -> None:  # noqa: ANN001
            self.context = context

        def on(self, event, callback) -> None:  # noqa: ANN001
            return None

        def title(self) -> str:
            return "Live"

        def is_closed(self) -> bool:
            return False

    context = _Context()
    page = _Page(context)
    context.pages.append(page)
    device = PlaywrightDevice(page=page, artifact_dir=tmp_path)
    frames = []

    device.add_preview_listener(lambda observation, page_id: frames.append((observation, page_id)))
    context.session.listeners["Page.screencastFrame"](
        {
            "sessionId": 7,
            "data": base64.b64encode(b"pushed-frame").decode("ascii"),
        }
    )

    assert context.session.sent[0][0] == "Page.startScreencast"
    assert context.session.sent[-1] == (
        "Page.screencastFrameAck",
        {"sessionId": 7},
    )
    assert Path(frames[0][0].screenshot_path).read_bytes() == b"pushed-frame"
    assert frames[0][1] == "page_1"


def test_playwright_wait_without_arguments_uses_shared_default() -> None:
    page = FakeExecutionPage()

    result = PlaywrightDevice(page=page).execute(Action("wait", ActionChannel.BROWSER))

    assert result.status is ActionStatus.SUCCESS
    assert ("wait", 1000.0) in page.performed


class FailingLocator:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def count(self) -> int:
        return 1

    def click(self) -> None:
        raise self.error


class FailingPage:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def locator(self, _selector: str) -> FailingLocator:
        return FailingLocator(self.error)


def test_playwright_device_maps_execution_errors_to_action_results() -> None:
    action = Action(
        "click",
        ActionChannel.BROWSER,
        locator=Locator("css", "#save"),
    )
    timed_out = PlaywrightDevice(page=FailingPage(TimeoutError("slow"))).execute(action)
    failed = PlaywrightDevice(page=FailingPage(RuntimeError("detached"))).execute(action)
    blocked = PlaywrightDevice(page=object()).execute(
        Action("click", ActionChannel.EXTERNAL, locator=action.locator)
    )

    assert (timed_out.status, timed_out.error) == (
        ActionStatus.TIMEOUT,
        "slow",
    )
    assert (failed.status, failed.error) == (
        ActionStatus.FAILED,
        "detached",
    )
    assert blocked.status is ActionStatus.BLOCKED


def test_playwright_device_exposes_specs_for_every_supported_action() -> None:
    specs = PlaywrightDevice.action_specs()

    assert [spec.name for spec in specs] == [
        "navigate",
        "click",
        "download",
        "fill",
        "press",
        "select",
        "check",
        "hover",
        "wait",
        "assert_text",
        "assert_visible",
        "extract_value",
        "screenshot",
        "bind_page",
        "switch_page",
        "close_page",
        "open_popup",
    ]
    assert all(spec.channel is ActionChannel.BROWSER for spec in specs)
