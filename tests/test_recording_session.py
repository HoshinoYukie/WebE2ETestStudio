from pathlib import Path

from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    Locator,
    StepSource,
    VerificationResult,
    VerificationStatus,
)
from web_e2e_test_studio.recording import RecordingSession
from web_e2e_test_studio.storage import JsonAssetStore


def test_recording_session_only_commits_verified_actions(tmp_path: Path) -> None:
    session = RecordingSession.create(
        name="search",
        goal="Search for invoices",
        base_url="https://example.test",
    )

    failed = session.stage(
        Action(
            name="click",
            channel=ActionChannel.BROWSER,
            locator=Locator("role", "button", name="Search"),
        ),
        intent="Open search",
        expected_result="Search form is visible",
        source=StepSource.VLM,
    )
    committed = session.resolve(
        failed.id,
        ActionResult.failed("Element was detached"),
        VerificationResult(
            status=VerificationStatus.REJECTED,
            reason="The form did not open",
        ),
    )

    assert committed is None
    assert session.asset.steps == []
    assert session.attempts[0].status.value == "rejected"

    successful = session.stage(
        Action(
            name="fill",
            channel=ActionChannel.BROWSER,
            locator=Locator("label", "Search"),
            arguments={"text": "invoice 123"},
        ),
        intent="Enter an invoice query",
        expected_result="The query is present in the search field",
        source=StepSource.MANUAL,
    )
    committed = session.resolve(
        successful.id,
        ActionResult.succeeded({"value": "invoice 123"}),
        VerificationResult(
            status=VerificationStatus.VERIFIED,
            reason="The field contains the requested query",
            evidence={"value": "invoice 123"},
        ),
    )

    assert committed is not None
    assert [step.action.name for step in session.asset.steps] == ["fill"]
    assert session.asset.steps[0].verification.reason.startswith("The field")

    snapshot = tmp_path / "search.asset.json"
    store = JsonAssetStore()
    store.save(session.asset, snapshot)

    restored = store.load(snapshot)
    assert restored.to_dict() == session.asset.to_dict()


def test_recording_session_commits_the_deterministically_selected_locator() -> None:
    session = RecordingSession.create(name="save", goal="Save changes")
    candidate = session.stage(
        Action(
            "click",
            ActionChannel.BROWSER,
            locator=Locator("text", "Save"),
        ),
        intent="Save changes",
        expected_result="Changes are saved",
        source=StepSource.VLM,
    )
    result = ActionResult.succeeded(
        {
            "locator_resolution": {
                "selected": Locator("test_id", "save").to_dict(),
                "candidates": [],
            }
        }
    )

    step = session.resolve(
        candidate.id,
        result,
        VerificationResult(
            VerificationStatus.VERIFIED,
            "Changes were saved",
        ),
    )

    assert step is not None
    assert step.action.locator == Locator(
        "test_id",
        "save",
        fallbacks=(Locator("text", "Save"),),
    )
