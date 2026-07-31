from pathlib import Path

from web_e2e_test_studio.browser import PlaywrightDevice
from web_e2e_test_studio.context import (
    ContextEngine,
    ContextPurpose,
    ContextRequest,
)


class FakeFrame:
    def __init__(self, url: str, name: str = "") -> None:
        self.url = url
        self.name = name


class FakeBrowserContext:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.listeners = {}

    def on(self, event: str, callback) -> None:  # noqa: ANN001
        self.listeners[event] = callback

    def emit_page(self, page: "FakePage") -> None:
        self.listeners["page"](page)


class FakePage:
    def __init__(self, context: FakeBrowserContext) -> None:
        self.url = "https://example.test/account?token=secret"
        self.context = context
        self.frames = [
            FakeFrame(self.url),
            FakeFrame("https://example.test/embed", "details"),
        ]
        self.evaluate_calls = 0
        self.waits: list[tuple[str, int]] = []
        self.listeners = {}
        self.page_text = (
            "Account\npassword: hunter2\nAuthorization: Bearer abc\nSave"
        )
        context.pages.append(self)

    def on(self, event: str, callback) -> None:  # noqa: ANN001
        self.listeners[event] = callback

    def emit(self, event: str) -> None:
        self.listeners[event]()

    def title(self) -> str:
        return "Account"

    def evaluate(self, script: str):  # noqa: ANN201
        assert "interactive_elements" in script
        self.evaluate_calls += 1
        return {
            "viewport": {"width": 1280, "height": 720},
            "interactive_elements": [
                {
                    "tag": "input",
                    "role": "",
                    "name": "Password",
                    "text": "should-not-leak",
                    "test_id": "password",
                    "input_type": "password",
                },
                {
                    "tag": "button",
                    "role": "button",
                    "name": "Save",
                    "text": "Save",
                    "test_id": "save",
                    "input_type": "",
                },
            ],
            "page_text": self.page_text,
        }

    def screenshot(self, *, path: str, full_page: bool) -> None:
        assert full_page is False
        Path(path).write_bytes(b"png")

    def wait_for_load_state(self, state: str, *, timeout: int) -> None:
        self.waits.append((state, timeout))


def test_context_engine_captures_rich_sanitized_context(tmp_path: Path) -> None:
    browser_context = FakeBrowserContext()
    page = FakePage(browser_context)
    second_page = FakePage(browser_context)
    second_page.url = "https://example.test/second"
    engine = ContextEngine(
        lambda: page,
        artifact_dir=tmp_path,
        max_page_text=100,
    )

    context = engine.capture(
        ContextRequest(ContextPurpose.INSIGHT, include_screenshot=True)
    )

    assert context.url == "https://example.test/account?token=%5BREDACTED%5D"
    assert context.page_count == 2
    assert context.active_page_index == 0
    assert [tab.active for tab in context.tabs] == [True, False]
    assert context.tabs[1].url == "https://example.test/second"
    assert [frame.name for frame in context.frames] == ["", "details"]
    assert context.interactive_elements[0]["text"] == ""
    assert "hunter2" not in context.page_text
    assert "Bearer abc" not in context.page_text
    assert len(context.fingerprint) == 64
    assert Path(context.screenshot_path).read_bytes() == b"png"


def test_context_engine_reuses_planning_context_and_refreshes_insight() -> None:
    browser_context = FakeBrowserContext()
    page = FakePage(browser_context)
    engine = ContextEngine(lambda: page)

    first = engine.capture(ContextRequest(ContextPurpose.PLANNING))
    reused = engine.capture(ContextRequest(ContextPurpose.PLANNING))
    refreshed = engine.capture(ContextRequest(ContextPurpose.INSIGHT))
    page.page_text = "Account changed without interactive DOM changes"
    changed = engine.capture(ContextRequest(ContextPurpose.INSIGHT))

    assert reused is first
    assert refreshed is not first
    assert refreshed.changed_since_previous is False
    assert changed.changed_since_previous is True
    assert changed.previous_fingerprint == refreshed.fingerprint
    assert page.evaluate_calls == 3


def test_context_engine_does_not_mix_screenshot_cache_variants(
    tmp_path: Path,
) -> None:
    browser_context = FakeBrowserContext()
    page = FakePage(browser_context)
    engine = ContextEngine(lambda: page, artifact_dir=tmp_path)

    without = engine.capture(
        ContextRequest(
            ContextPurpose.PLANNING,
            include_screenshot=False,
        )
    )
    with_screenshot = engine.capture(
        ContextRequest(
            ContextPurpose.PLANNING,
            include_screenshot=True,
        )
    )

    assert without.screenshot_path == ""
    assert with_screenshot.screenshot_path
    assert page.evaluate_calls == 2


def test_context_engine_expires_planning_cache() -> None:
    browser_context = FakeBrowserContext()
    page = FakePage(browser_context)
    now = [10.0]
    engine = ContextEngine(
        lambda: page,
        planning_cache_ttl_ms=500,
        clock=lambda: now[0],
    )

    first = engine.capture(ContextRequest(ContextPurpose.PLANNING))
    now[0] += 0.6
    expired = engine.capture(ContextRequest(ContextPurpose.PLANNING))

    assert expired is not first
    assert page.evaluate_calls == 2


def test_context_engine_freeze_has_priority_over_forced_refresh() -> None:
    browser_context = FakeBrowserContext()
    page = FakePage(browser_context)
    engine = ContextEngine(lambda: page)

    frozen = engine.freeze()
    page.url = "https://example.test/changed"
    still_frozen = engine.capture(
        ContextRequest(ContextPurpose.INSIGHT, force_refresh=True)
    )
    engine.unfreeze()
    refreshed = engine.capture(ContextRequest(ContextPurpose.INSIGHT))

    assert still_frozen is frozen
    assert refreshed.url == "https://example.test/changed"


class NavigatingPage(FakePage):
    def evaluate(self, script: str):  # noqa: ANN201
        if self.evaluate_calls == 0:
            self.evaluate_calls += 1
            raise RuntimeError(
                "Page.evaluate: Execution context was destroyed, "
                "most likely because of a navigation"
            )
        return super().evaluate(script)


def test_context_engine_retries_navigation_context_errors() -> None:
    browser_context = FakeBrowserContext()
    page = NavigatingPage(browser_context)
    engine = ContextEngine(lambda: page)

    context = engine.capture(ContextRequest(ContextPurpose.INSIGHT))

    assert context.title == "Account"
    assert page.evaluate_calls == 2
    assert page.waits == [("domcontentloaded", 5_000)]


def test_playwright_device_tracks_new_popup_as_active_page() -> None:
    browser_context = FakeBrowserContext()
    first = FakePage(browser_context)
    device = PlaywrightDevice(page=first)
    popup = FakePage(browser_context)
    popup.url = "https://example.test/popup"

    browser_context.emit_page(popup)
    context = device.capture_context(
        ContextRequest(ContextPurpose.INSIGHT, include_screenshot=False)
    )

    assert device.page is popup
    assert context.url == "https://example.test/popup"
    assert context.active_page_index == 1

    popup.emit("close")
    assert device.page is first


def test_playwright_device_exposes_stable_tabs_and_can_switch_page() -> None:
    browser_context = FakeBrowserContext()
    first = FakePage(browser_context)
    first.url = "https://example.test/first"
    device = PlaywrightDevice(page=first)
    popup = FakePage(browser_context)
    popup.url = "https://example.test/popup"

    browser_context.emit_page(popup)
    tabs = device.pages()

    assert len(tabs) == 2
    assert tabs[0].id != tabs[1].id
    assert tabs[1].active is True
    device.activate_page(tabs[0].id)

    assert device.page is first
    assert device.pages()[0].active is True


def test_context_engine_bounds_tabs_but_retains_active_page() -> None:
    browser_context = FakeBrowserContext()
    pages = [FakePage(browser_context) for _ in range(21)]
    active = pages[-1]
    active.url = "https://example.test/active"
    engine = ContextEngine(lambda: active, max_tabs=5)

    context = engine.capture(ContextRequest(ContextPurpose.INSIGHT))

    assert context.page_count == 21
    assert context.active_page_index == 20
    assert len(context.tabs) == 5
    assert context.tabs[-1].active is True
    assert context.tabs[-1].url == "https://example.test/active"
