from collections.abc import Sequence

from web_e2e_test_studio.agent import (
    ActionRegistry,
    ActionSpec,
    AuthoringAgent,
    PlannedAction,
    PlanningResult,
    RunStatus,
)
from web_e2e_test_studio.context import (
    ContextPurpose,
    ContextRequest,
    UIContext,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    ActionStatus,
    Locator,
    Observation,
    VerificationResult,
    VerificationStatus,
)
from web_e2e_test_studio.recording import RecordingSession


class FakeObserver:
    def __init__(self) -> None:
        self.calls = 0

    def observe(self) -> Observation:
        self.calls += 1
        return Observation(
            url=f"https://example.test/state-{self.calls}",
            title=f"State {self.calls}",
        )


class RichObserver(FakeObserver):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[ContextRequest] = []

    def capture_context(self, request: ContextRequest) -> UIContext:
        self.requests.append(request)
        self.calls += 1
        return UIContext(
            url=f"https://example.test/context-{self.calls}",
            fingerprint=f"fingerprint-{self.calls}",
        )


class SequencedPlanner:
    def __init__(self, plans: Sequence[PlanningResult]) -> None:
        self.plans = list(plans)
        self.feedback_lengths: list[int] = []

    def plan(self, request, observation, history, actions):  # noqa: ANN001
        self.feedback_lengths.append(len(history))
        return self.plans.pop(0)


class CapturingPlanner:
    def __init__(self) -> None:
        self.action_names: list[str] = []

    def plan(self, request, observation, history, actions):  # noqa: ANN001
        self.action_names = [action.name for action in actions]
        return PlanningResult.complete("No action required")


class SequencedExecutor:
    def __init__(self, results: Sequence[ActionResult]) -> None:
        self.results = list(results)
        self.actions: list[Action] = []

    def execute(self, action: Action) -> ActionResult:
        self.actions.append(action)
        return self.results.pop(0)


class ResultVerifier:
    def verify(
        self,
        action: Action,
        result: ActionResult,
        before: Observation,
        after: Observation,
        expected_result: str,
    ) -> VerificationResult:
        if not result.success:
            return VerificationResult(
                VerificationStatus.REJECTED,
                f"Execution failed: {result.error}",
            )
        return VerificationResult(
            VerificationStatus.VERIFIED,
            "The deterministic action succeeded and the page was re-observed",
            evidence={"before_url": before.url, "after_url": after.url},
        )


def _click_plan(intent: str) -> PlanningResult:
    return PlanningResult.actions(
        (
            PlannedAction(
                action=Action(
                    name="click",
                    channel=ActionChannel.BROWSER,
                    locator=Locator("role", "button", name="Search"),
                ),
                intent=intent,
                expected_result="Search opens",
            ),
        )
    )


def test_authoring_agent_hides_user_initiated_actions_from_planner() -> None:
    planner = CapturingPlanner()
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            "launch_application",
            ActionChannel.EXTERNAL,
            "Launch manually",
            user_initiated_only=True,
        ),
        SequencedExecutor([ActionResult.succeeded()]),
    )

    AuthoringAgent(
        observer=FakeObserver(),
        planner=planner,
        verifier=ResultVerifier(),
        actions=registry,
    ).perform(
        RecordingSession.create(name="manual", goal="Launch manually"),
        "Wait for the user",
    )

    assert planner.action_names == []


def test_registry_blocks_manual_only_action_without_user_authorization() -> None:
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            "launch_application",
            ActionChannel.EXTERNAL,
            "Launch manually",
            user_initiated_only=True,
        ),
        SequencedExecutor([ActionResult.succeeded({"pid": 1})]),
    )

    result = registry.execute(
        Action("launch_application", ActionChannel.EXTERNAL)
    )

    assert result.status is ActionStatus.BLOCKED
    assert result.error_category == "authorization"


def test_agent_replans_after_failure_and_only_records_verified_step() -> None:
    observer = FakeObserver()
    planner = SequencedPlanner(
        [
            _click_plan("Try the visible search button"),
            _click_plan("Retry with the stable search button"),
            PlanningResult.complete("Search is open"),
        ]
    )
    executor = SequencedExecutor(
        [ActionResult.failed("detached"), ActionResult.succeeded()]
    )
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            name="click",
            channel=ActionChannel.BROWSER,
            description="Click a located element",
            locator_required=True,
        ),
        executor,
    )
    session = RecordingSession.create(
        name="search",
        goal="Open search",
        base_url="https://example.test",
    )
    agent = AuthoringAgent(
        observer=observer,
        planner=planner,
        verifier=ResultVerifier(),
        actions=registry,
        max_rounds=5,
    )

    result = agent.perform(session, "Open search")

    assert result.status is RunStatus.COMPLETED
    assert result.summary == "Search is open"
    assert len(session.attempts) == 2
    assert [step.intent for step in session.asset.steps] == [
        "Retry with the stable search button"
    ]
    assert planner.feedback_lengths == [0, 1, 2]
    assert [event.kind for event in result.events].count("step_committed") == 1


def test_agent_returns_structured_question_without_executing() -> None:
    observer = FakeObserver()
    planner = SequencedPlanner(
        [
            PlanningResult.ask("Which account should be used?")
        ]
    )
    registry = ActionRegistry()
    session = RecordingSession.create(name="login", goal="Log in")

    result = AuthoringAgent(
        observer=observer,
        planner=planner,
        verifier=ResultVerifier(),
        actions=registry,
    ).perform(session, "Log in")

    assert result.status is RunStatus.WAITING_FOR_USER
    assert result.question == "Which account should be used?"
    assert session.asset.steps == []


def test_agent_uses_planning_and_fresh_insight_context_purposes() -> None:
    observer = RichObserver()
    planner = SequencedPlanner(
        [_click_plan("Open"), PlanningResult.complete("Done")]
    )
    executor = SequencedExecutor([ActionResult.succeeded()])
    registry = ActionRegistry()
    registry.register(
        ActionSpec(
            "click",
            ActionChannel.BROWSER,
            "Click",
            locator_required=True,
        ),
        executor,
    )

    AuthoringAgent(
        observer=observer,
        planner=planner,
        verifier=ResultVerifier(),
        actions=registry,
    ).perform(
        RecordingSession.create(name="context", goal="Use rich context"),
        "Open",
    )

    assert [request.purpose for request in observer.requests] == [
        ContextPurpose.PLANNING,
        ContextPurpose.INSIGHT,
        ContextPurpose.PLANNING,
    ]
    assert observer.requests[1].force_refresh is True
