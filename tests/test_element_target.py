from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ElementFingerprint,
    ElementTarget,
    FrameRef,
    Locator,
)


def test_element_target_round_trips_with_frame_and_ranked_locators() -> None:
    target = ElementTarget(
        page_ref="popup_1",
        frame_path=(
            FrameRef(selector="#content-frame", name="content", url_hint="/embedded"),
        ),
        candidates=(
            Locator("test_id", "create-proposal"),
            Locator("role", "link", name="Create Proposal"),
        ),
        fingerprint=ElementFingerprint(
            tag="a",
            role="link",
            accessible_name="Create Proposal",
            attributes={"data-testid": "create-proposal"},
        ),
    )
    action = Action("click", ActionChannel.BROWSER, target=target)

    restored = Action.from_dict(action.to_dict())

    assert restored.target == target
    assert restored.locator == Locator(
        "test_id",
        "create-proposal",
        fallbacks=(Locator("role", "link", name="Create Proposal"),),
    )


def test_legacy_locator_action_is_exposed_as_an_element_target() -> None:
    locator = Locator("role", "button", name="Save")

    restored = Action.from_dict(
        {
            "name": "click",
            "channel": "browser",
            "arguments": {},
            "locator": locator.to_dict(),
        }
    )

    assert restored.locator == locator
    assert restored.target == ElementTarget(candidates=(locator,))
