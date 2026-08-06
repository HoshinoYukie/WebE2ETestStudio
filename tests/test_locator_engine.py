from __future__ import annotations

import os

import pytest

from web_e2e_test_studio.browser import PlaywrightDevice
from web_e2e_test_studio.browser.locator import (
    LocatorEngine,
    LocatorProbe,
    LocatorResolutionError,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    Locator,
    Observation,
)


def test_locator_engine_prefers_unique_stable_dom_candidate() -> None:
    observation = Observation(
        interactive_elements=(
            {
                "tag": "button",
                "role": "button",
                "name": "Export All",
                "text": "Export All",
                "test_id": "export-all",
                "visible": True,
                "disabled": False,
            },
        )
    )
    probes = {
        ("text", "Export All"): LocatorProbe(count=1, visible=True),
        ("test_id", "export-all"): LocatorProbe(count=1, visible=True),
        ("role", "button"): LocatorProbe(count=1, visible=True),
    }

    resolution = LocatorEngine().resolve(
        Locator("text", "Export All"),
        observation,
        lambda locator: probes[(locator.strategy, locator.value)],
    )

    assert resolution.selected == Locator("test_id", "export-all")
    assert resolution.candidates[0].source == "observation:test_id"
    assert resolution.candidates[0].viable is True


def test_locator_engine_rejects_ambiguous_candidate_and_uses_unique_role() -> None:
    observation = Observation(
        interactive_elements=(
            {
                "tag": "button",
                "role": "button",
                "name": "Save",
                "text": "Save",
                "test_id": "action",
                "visible": True,
                "disabled": False,
            },
        )
    )

    resolution = LocatorEngine().resolve(
        Locator("text", "Save"),
        observation,
        lambda locator: (
            LocatorProbe(count=3, visible=True)
            if locator.strategy == "test_id"
            else LocatorProbe(count=1, visible=True)
        ),
    )

    assert resolution.selected == Locator(
        "role", "button", name="Save"
    )
    rejected = next(
        item
        for item in resolution.candidates
        if item.locator.strategy == "test_id"
    )
    assert rejected.viable is False
    assert rejected.reason == "matched 3 elements"


def test_locator_engine_rejects_visible_candidate_that_cannot_receive_clicks() -> None:
    requested = Locator("role", "link", name="Create Proposal")
    observation = Observation(
        interactive_elements=(
            {
                "role": "link",
                "name": "Create Proposal",
                "text": "Create Proposal",
            },
        )
    )

    resolution = LocatorEngine().resolve(
        requested,
        observation,
        lambda locator: LocatorProbe(
            count=1,
            visible=True,
            enabled=True,
            actionable=locator.strategy == "text",
        ),
    )

    assert resolution.selected.strategy == "text"
    role = next(
        item for item in resolution.candidates if item.locator.strategy == "role"
    )
    assert role.viable is False
    assert "pointer" in role.reason


def test_locator_engine_returns_auditable_failure() -> None:
    with pytest.raises(LocatorResolutionError) as raised:
        LocatorEngine().resolve(
            Locator(
                "role",
                "button",
                name="Missing",
                fallbacks=(Locator("test_id", "missing"),),
            ),
            Observation(),
            lambda locator: LocatorProbe(count=0, visible=False),
        )

    assert "role='button'" in str(raised.value)
    assert "test_id='missing'" in str(raised.value)
    assert len(raised.value.candidates) == 2


@pytest.mark.browser
def test_playwright_device_uses_observation_to_upgrade_locator() -> None:
    if os.environ.get("RUN_BROWSER_TESTS") != "1":
        pytest.skip("set RUN_BROWSER_TESTS=1 after installing Chromium")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(
            '<button data-testid="save-button" '
            'onclick="this.dataset.clicked = \'yes\'">Save</button>'
        )
        device = PlaywrightDevice(page=page)
        device.observe()

        result = device.execute(
            Action(
                "click",
                ActionChannel.BROWSER,
                locator=Locator("text", "Save"),
            )
        )

        assert result.success
        resolution = result.output["locator_resolution"]
        assert isinstance(resolution, dict)
        assert resolution["selected"]["strategy"] == "test_id"
        assert page.get_by_test_id("save-button").get_attribute(
            "data-clicked"
        ) == "yes"
        browser.close()


@pytest.mark.browser
def test_playwright_device_clicks_visible_element_inside_iframe() -> None:
    """A visually present frame target must not be treated as absent from the page."""
    if os.environ.get("RUN_BROWSER_TESTS") != "1":
        pytest.skip("set RUN_BROWSER_TESTS=1 after installing Chromium")

    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        page = browser.new_page()
        page.set_content(
            "<iframe srcdoc=\"<a href='#proposal' "
            "onclick=&quot;this.dataset.clicked='yes';return false&quot;>"
            "Create Proposal</a>\"></iframe>"
        )
        device = PlaywrightDevice(page=page)
        device.observe()

        result = device.execute(
            Action(
                "click",
                ActionChannel.BROWSER,
                locator=Locator("role", "link", name="Create Proposal"),
            )
        )

        assert result.success, result.error
        assert (
            page.frames[1]
            .get_by_role("link", name="Create Proposal")
            .get_attribute("data-clicked")
            == "yes"
        )
        browser.close()
