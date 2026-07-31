import json

import pytest

from web_e2e_test_studio.agent import (
    ActionSpec,
    ExecutionDump,
    ExecutionRecord,
    ExecutionTask,
    PlannedAction,
    PlanningResult,
    PlanningStatus,
    TaskStatus,
)
from web_e2e_test_studio.agent.loop import AgentFeedback
from web_e2e_test_studio.agent.planner import (
    PlannerProtocolError,
    StructuredVLMPlanner,
)
from web_e2e_test_studio.agent.verification import DeterministicVerifier
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    Locator,
    Observation,
    VerificationResult,
    VerificationStatus,
)


def test_vlm_backend_can_ignore_proxy_environment(
    monkeypatch,
) -> None:  # noqa: ANN001
    import web_e2e_test_studio.agent.planner as planner_module

    client_options = {}
    openai_options = {}

    class FakeHttpClient:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            client_options.update(kwargs)

    class FakeOpenAI:
        def __init__(self, **kwargs) -> None:  # noqa: ANN003
            openai_options.update(kwargs)

    monkeypatch.setenv("VLM_API_KEY", "secret")
    monkeypatch.setenv("VLM_IGNORE_PROXY", "true")
    monkeypatch.setattr(planner_module, "DefaultHttpxClient", FakeHttpClient)
    monkeypatch.setattr(planner_module, "OpenAI", FakeOpenAI)

    planner_module.OpenAICompatibleVLMBackend.from_env()

    assert client_options == {"trust_env": False}
    assert isinstance(openai_options["http_client"], FakeHttpClient)


class FakeBackend:
    def __init__(self, response: dict) -> None:
        self.response = response
        self.calls: list[tuple[str, str, str]] = []

    def complete_json(
        self, system_prompt: str, user_prompt: str, screenshot_path: str
    ) -> dict:
        self.calls.append((system_prompt, user_prompt, screenshot_path))
        return self.response


class SequenceBackend(FakeBackend):
    def __init__(
        self,
        responses: list[dict | Exception],
    ) -> None:
        super().__init__({})
        self.responses = list(responses)

    def complete_json(
        self, system_prompt: str, user_prompt: str, screenshot_path: str
    ) -> dict:
        self.calls.append((system_prompt, user_prompt, screenshot_path))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_vlm_planner_recovers_from_one_non_json_response() -> None:
    backend = SequenceBackend(
        [
            PlannerProtocolError("VLM did not return valid JSON"),
            {
                "status": "complete",
                "summary": "The requested page is ready.",
            },
        ]
    )
    planner = StructuredVLMPlanner(backend)

    plan = planner.plan(
        "Wait until the page is ready",
        Observation(url="https://example.test/devices"),
        history=[],
        actions=[],
    )

    assert plan.status is PlanningStatus.COMPLETE
    assert len(backend.calls) == 2
    assert "valid JSON" in backend.calls[1][1]


def test_vlm_planner_parses_and_reinjects_working_memory() -> None:
    backend = SequenceBackend(
        [
            {
                "status": "ask",
                "question": "Which worksheet?",
                "memory": "Downloaded file is C:\\downloads\\devices.xlsx",
            },
            {"status": "complete", "summary": "Workbook inspected"},
        ]
    )
    planner = StructuredVLMPlanner(backend)

    first = planner.plan("Inspect workbook", Observation(), [], [])
    planner.set_memories((first.memory,))
    planner.plan("Inspect workbook", Observation(), [], [])

    assert first.memory == "Downloaded file is C:\\downloads\\devices.xlsx"
    assert "secret values may be remembered" in backend.calls[0][0]
    second_prompt = json.loads(backend.calls[1][1])
    assert second_prompt["working_memory"] == [
        "Downloaded file is C:\\downloads\\devices.xlsx"
    ]


def test_open_remembered_link_uses_exact_url_instead_of_visual_guess() -> None:
    planner = StructuredVLMPlanner(
        FakeBackend(
            {
                "status": "actions",
                "actions": [
                    {
                        "intent": "Click a similar installation button",
                        "expected_result": "Installation page opens",
                        "action": {
                            "name": "click",
                            "channel": "browser",
                            "arguments": {},
                            "locator": {
                                "strategy": "role",
                                "value": "button",
                                "name": "Send Installation Request",
                            },
                        },
                    }
                ],
            }
        )
    )
    planner.set_memories(
        (
            "bulk installation link: "
            "https://install.example.test/bulk/contract-289886",
        )
    )

    plan = planner.plan(
        "打开这个 bulk installation link",
        Observation(url="https://example.test/devices"),
        [],
        [
            ActionSpec(
                "navigate",
                ActionChannel.BROWSER,
                "Navigate",
                required_arguments=("url",),
                argument_schema={"url": "string"},
            ),
            ActionSpec(
                "click",
                ActionChannel.BROWSER,
                "Click",
                locator_required=True,
            ),
        ],
    )

    action = plan.planned_actions[0].action
    assert action.name == "navigate"
    assert action.arguments["url"] == (
        "https://install.example.test/bulk/contract-289886"
    )


def test_open_remembered_link_asks_when_memory_has_multiple_matches() -> None:
    planner = StructuredVLMPlanner(
        FakeBackend({"status": "complete", "summary": "Opened"})
    )
    planner.set_memories(
        (
            "bulk installation link for A: https://install.example.test/a",
            "bulk installation link for B: https://install.example.test/b",
        )
    )

    plan = planner.plan(
        "打开 bulk installation link",
        Observation(),
        [],
        [
            ActionSpec(
                "navigate",
                ActionChannel.BROWSER,
                "Navigate",
                required_arguments=("url",),
                argument_schema={"url": "string"},
            )
        ],
    )

    assert plan.status is PlanningStatus.ASK
    assert "https://install.example.test/a" in plan.question
    assert "https://install.example.test/b" in plan.question


def test_vlm_planner_parses_only_registered_structured_action() -> None:
    backend = FakeBackend(
        {
            "status": "actions",
            "thought": "The Search button is visible",
            "confidence": 0.92,
            "actions": [
                {
                    "intent": "Open search",
                    "expected_result": "Search panel opens",
                    "action": {
                        "name": "click",
                        "channel": "browser",
                        "locator": {
                            "strategy": "role",
                            "value": "button",
                            "name": "Search",
                        },
                        "arguments": {},
                    },
                },
            ],
        }
    )
    planner = StructuredVLMPlanner(backend)

    plan = planner.plan(
        "Open search",
        Observation(
            url="https://example.test",
            screenshot_path="shot.png",
            interactive_elements=(
                {"role": "button", "name": "Search", "test_id": "search"},
            ),
        ),
        history=[],
        actions=[
            ActionSpec(
                name="click",
                channel=ActionChannel.BROWSER,
                description="Click an element",
                locator_required=True,
            )
        ],
    )

    assert plan.status is PlanningStatus.ACTIONS
    assert plan.planned_actions[0].action == Action(
        name="click",
        channel=ActionChannel.BROWSER,
        locator=Locator("role", "button", name="Search"),
    )
    assert backend.calls[0][2] == "shot.png"
    assert '"name": "click"' in backend.calls[0][0]


def test_vlm_planner_parses_ordered_multi_channel_action_plan() -> None:
    planner = StructuredVLMPlanner(
        FakeBackend(
            {
                "status": "actions",
                "thought": "Fetch data, then wait for the page",
                "actions": [
                    {
                        "intent": "Load device data",
                        "expected_result": "API responds",
                        "action": {
                            "name": "http_request",
                            "channel": "api",
                            "arguments": {
                                "method": "GET",
                                "url": "https://example.test/devices",
                            },
                        },
                    },
                    {
                        "intent": "Wait for rendering",
                        "expected_result": "Page settles",
                        "action": {
                            "name": "wait",
                            "channel": "browser",
                            "arguments": {"milliseconds": 500},
                        },
                    },
                ],
            }
        )
    )

    plan = planner.plan(
        "Load devices",
        Observation(),
        history=[],
        actions=[
            ActionSpec(
                "http_request",
                ActionChannel.API,
                "Issue HTTP request",
                required_arguments=("method", "url"),
            ),
            ActionSpec("wait", ActionChannel.BROWSER, "Wait"),
        ],
    )

    assert [item.action.channel for item in plan.planned_actions] == [
        ActionChannel.API,
        ActionChannel.BROWSER,
    ]
    assert [item.intent for item in plan.planned_actions] == [
        "Load device data",
        "Wait for rendering",
    ]


def test_vlm_planner_rejects_unregistered_model_action() -> None:
    planner = StructuredVLMPlanner(
        FakeBackend(
            {
                "status": "actions",
                "actions": [
                    {
                        "action": {
                            "name": "python",
                            "channel": "external",
                            "arguments": {"code": "do_anything()"},
                        },
                    }
                ],
            }
        )
    )

    with pytest.raises(PlannerProtocolError, match="not registered"):
        planner.plan(
            "Do it",
            Observation(),
            history=[],
            actions=[],
        )


def test_vlm_planner_rejects_legacy_wait_decision() -> None:
    planner = StructuredVLMPlanner(
        FakeBackend(
            {
                "decision": "wait",
                "thought": "The next page is still loading",
                "milliseconds": 2500,
            }
        )
    )
    wait_spec = ActionSpec(
        name="wait",
        channel=ActionChannel.BROWSER,
        description="Wait for the page",
    )

    with pytest.raises(PlannerProtocolError, match="plan status"):
        planner.plan(
            "Continue after loading",
            Observation(url="https://example.test/loading"),
            history=[],
            actions=[wait_spec],
        )


def test_vlm_planner_requires_explicit_ask_status() -> None:
    planner = StructuredVLMPlanner(
        FakeBackend(
            {
                "decision": "wait",
                "question": "Please finish the CAPTCHA, then continue.",
            }
        )
    )

    with pytest.raises(PlannerProtocolError, match="plan status"):
        planner.plan(
            "Log in",
            Observation(),
            history=[],
            actions=[],
        )


def _successful_download_feedback() -> AgentFeedback:
    action = Action("download", ActionChannel.BROWSER)
    task = ExecutionTask(
        "task_download",
        action,
        "Download devices",
        "File is saved",
    )
    result = ActionResult.succeeded(
        {
            "filename": "devices.xlsx",
            "path": r"C:\downloads\devices.xlsx",
        }
    )
    verification = VerificationResult(
        VerificationStatus.VERIFIED,
        "Download completed",
    )
    record = ExecutionRecord(
        task,
        TaskStatus.SUCCEEDED,
        Observation(),
        Observation(),
        result,
        verification,
    )
    previous_plan = PlanningResult.actions(
        (PlannedAction(action, "Download devices", "File is saved"),)
    )
    return AgentFeedback(
        previous_plan,
        ExecutionDump(TaskStatus.SUCCEEDED, (record,), ()),
    )


def _successful_wait_feedback() -> AgentFeedback:
    action = Action(
        "wait",
        ActionChannel.BROWSER,
        arguments={"milliseconds": 1000},
    )
    task = ExecutionTask("task_wait", action, "Wait", "Page settles")
    record = ExecutionRecord(
        task,
        TaskStatus.SUCCEEDED,
        Observation(),
        Observation(),
        ActionResult.succeeded({"milliseconds": 1000}),
        VerificationResult(
            VerificationStatus.VERIFIED,
            "The page was re-observed",
        ),
    )
    return AgentFeedback(
        PlanningResult.actions(
            (PlannedAction(action, "Wait", "Page settles"),)
        ),
        ExecutionDump(TaskStatus.SUCCEEDED, (record,), ()),
    )


def test_download_only_goal_completes_instead_of_asking_to_open_file() -> None:
    planner = StructuredVLMPlanner(
        FakeBackend(
            {
                "status": "ask",
                "question": "Should I open the downloaded Excel file?",
            }
        )
    )

    plan = planner.plan(
        "下载 Devices XLSX 文件",
        Observation(),
        history=[_successful_download_feedback()],
        actions=[],
    )

    assert plan.status is PlanningStatus.COMPLETE
    assert plan.summary == r"Download completed: C:\downloads\devices.xlsx"


def test_download_completion_policy_preserves_explicit_analysis_goal() -> None:
    planner = StructuredVLMPlanner(
        FakeBackend(
            {
                "status": "ask",
                "question": "Which tool should inspect the file?",
            }
        )
    )

    plan = planner.plan(
        "下载并打开分析 Devices XLSX 文件",
        Observation(),
        history=[_successful_download_feedback()],
        actions=[],
    )

    assert plan.status is PlanningStatus.ASK


def test_file_extraction_goal_replans_when_model_completes_after_download() -> None:
    backend = SequenceBackend(
        [
            {
                "status": "complete",
                "summary": "The file was downloaded but not parsed.",
            },
            {
                "status": "actions",
                "actions": [
                    {
                        "intent": "Extract the requested Excel information",
                        "expected_result": "The requested values are returned",
                        "action": {
                            "name": "run_external_tool",
                            "channel": "external",
                            "tool_ref": "excel_reader",
                            "arguments": {
                                "args": [r"C:\downloads\devices.xlsx"]
                            },
                        },
                    }
                ],
            },
        ]
    )
    planner = StructuredVLMPlanner(backend)

    plan = planner.plan(
        "下载 Excel 文件并提取其中的 bulk installation link",
        Observation(),
        history=[_successful_download_feedback()],
        actions=[
            ActionSpec(
                "run_external_tool",
                ActionChannel.EXTERNAL,
                "Run excel_reader to extract information from Excel files",
            )
        ],
    )

    assert plan.status is PlanningStatus.ACTIONS
    assert plan.planned_actions[0].action.tool_ref == "excel_reader"
    assert len(backend.calls) == 2
    assert "completed too early" in backend.calls[1][1]


def test_download_goal_upgrades_export_click_to_managed_download() -> None:
    planner = StructuredVLMPlanner(
        FakeBackend(
            {
                "status": "actions",
                "actions": [
                    {
                        "intent": "Export all devices",
                        "expected_result": "The XLSX file is downloaded",
                        "action": {
                            "name": "click",
                            "channel": "browser",
                            "locator": {
                                "strategy": "role",
                                "value": "button",
                                "name": "Export All",
                            },
                            "arguments": {},
                        },
                    }
                ],
            }
        )
    )

    plan = planner.plan(
        "下载 Device XLSX 文件并提取其中的安装链接",
        Observation(),
        history=[],
        actions=[
            ActionSpec(
                "click",
                ActionChannel.BROWSER,
                "Click an element",
                locator_required=True,
            ),
            ActionSpec(
                "download",
                ActionChannel.BROWSER,
                "Download a file",
                locator_required=True,
            ),
        ],
    )

    assert plan.planned_actions[0].action.name == "download"
    assert plan.planned_actions[0].action.locator == Locator(
        "role", "button", name="Export All"
    )


def test_file_goal_replans_when_model_asks_for_known_download_path() -> None:
    backend = SequenceBackend(
        [
            {
                "status": "ask",
                "question": "请提供下载文件的完整路径。",
            },
            {
                "status": "actions",
                "actions": [
                    {
                        "intent": "Inspect the downloaded workbook",
                        "expected_result": "The link is extracted",
                        "action": {
                            "name": "run_external_tool",
                            "channel": "external",
                            "tool_ref": "excel_reader",
                            "arguments": {
                                "args": [r"C:\downloads\devices.xlsx"]
                            },
                        },
                    }
                ],
            },
        ]
    )
    planner = StructuredVLMPlanner(backend)

    plan = planner.plan(
        "下载并打开 Excel 文件，提取 bulk installation link",
        Observation(),
        history=[_successful_download_feedback()],
        actions=[
            ActionSpec(
                "run_external_tool",
                ActionChannel.EXTERNAL,
                "Read Excel files with excel_reader",
            )
        ],
    )

    assert plan.status is PlanningStatus.ACTIONS
    assert plan.planned_actions[0].action.tool_ref == "excel_reader"
    assert len(backend.calls) == 2
    assert json.dumps(r"C:\downloads\devices.xlsx") in backend.calls[1][1]


def test_file_goal_keeps_download_path_across_later_wait_round() -> None:
    backend = SequenceBackend(
        [
            {
                "status": "ask",
                "question": (
                    "下载文件 'Devices XLSX' 的完整路径是什么？"
                    "需要该路径才能打开文件并获取 bulk installation link。"
                ),
            },
            {
                "status": "actions",
                "actions": [
                    {
                        "intent": "Read the downloaded workbook",
                        "expected_result": "The requested link is extracted",
                        "action": {
                            "name": "run_external_tool",
                            "channel": "external",
                            "tool_ref": "excel_reader",
                            "arguments": {
                                "args": [r"C:\downloads\devices.xlsx"]
                            },
                        },
                    }
                ],
            },
        ]
    )

    plan = StructuredVLMPlanner(backend).plan(
        "下载 Devices XLSX，打开文件并获取 bulk installation link",
        Observation(url="https://example.test/devices"),
        history=[
            _successful_download_feedback(),
            _successful_wait_feedback(),
        ],
        actions=[
            ActionSpec(
                "run_external_tool",
                ActionChannel.EXTERNAL,
                "Read Excel files with excel_reader",
            )
        ],
    )

    assert plan.status is PlanningStatus.ACTIONS
    assert plan.planned_actions[0].action.tool_ref == "excel_reader"
    assert len(backend.calls) == 2
    assert json.dumps(r"C:\downloads\devices.xlsx") in backend.calls[1][1]


def test_empty_generic_tool_call_is_rewritten_to_spreadsheet_extraction() -> None:
    planner = StructuredVLMPlanner(
        FakeBackend(
            {
                "status": "actions",
                "actions": [
                    {
                        "intent": "Read the downloaded workbook",
                        "expected_result": "Extract the bulk installation link",
                        "action": {
                            "name": "run_external_tool",
                            "channel": "external",
                            "tool_ref": "echo_tool",
                            "arguments": {},
                        },
                    }
                ],
            }
        )
    )
    spreadsheet_spec = ActionSpec(
        "extract_spreadsheet",
        ActionChannel.EXTERNAL,
        "Extract matching values from an XLSX file",
        required_arguments=("file_path", "query"),
        argument_schema={
            "file_path": "string",
            "query": "string",
            "sheet": "string",
        },
    )

    plan = planner.plan(
        "下载 Excel 文件并获取 bulk installation link",
        Observation(),
        history=[_successful_download_feedback()],
        actions=[
            spreadsheet_spec,
            ActionSpec(
                "run_external_tool",
                ActionChannel.EXTERNAL,
                "Run echo_tool",
            ),
        ],
    )

    action = plan.planned_actions[0].action
    assert action.name == "extract_spreadsheet"
    assert action.tool_ref is None
    assert action.arguments == {
        "file_path": r"C:\downloads\devices.xlsx",
        "query": "Extract the bulk installation link",
    }


def test_deterministic_verifier_requires_success_and_reobservation() -> None:
    verifier = DeterministicVerifier()
    action = Action(
        name="navigate",
        channel=ActionChannel.BROWSER,
        arguments={"url": "https://example.test/dashboard"},
    )

    failed = verifier.verify(
        action,
        ActionResult.failed("navigation timeout"),
        Observation(url="https://example.test"),
        Observation(url="https://example.test"),
        "Dashboard opens",
    )
    verified = verifier.verify(
        action,
        ActionResult.succeeded(),
        Observation(url="https://example.test"),
        Observation(url="https://example.test/dashboard"),
        "Dashboard opens",
    )

    assert failed.status is VerificationStatus.REJECTED
    assert verified.status is VerificationStatus.VERIFIED
    assert verified.evidence["after_url"].endswith("/dashboard")
