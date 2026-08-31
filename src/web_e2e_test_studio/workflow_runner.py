"""Execute business-editable Markdown workflows through Playwright authoring."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from web_e2e_test_studio.agent import (
    AuthoringAgent,
    ExecutionRunner,
    PlanCompiler,
    PlannedAction,
    PlanningResult,
    RunStatus,
)
from web_e2e_test_studio.agent.planner import (
    OpenAICompatibleVLMBackend,
    StructuredVLMPlanner,
)
from web_e2e_test_studio.agent.verification import DeterministicVerifier
from web_e2e_test_studio.browser import PlaywrightDevice
from web_e2e_test_studio.capabilities import (
    CapabilityContext,
    default_capability_catalog,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    JsonValue,
    RecordedStep,
    StepSource,
)
from web_e2e_test_studio.exporters import PytestExporter
from web_e2e_test_studio.recording import RecordingSession
from web_e2e_test_studio.storage import JsonAssetStore
from web_e2e_test_studio.tools import ExternalToolRegistry
from web_e2e_test_studio.workflow_spec import CompiledWorkflow, MarkdownWorkflowSpec


@dataclass(frozen=True)
class WorkflowRunResult:
    status: RunStatus
    asset_path: Path
    output_path: Path
    summary: str = ""
    question: str = ""

    @property
    def completed(self) -> bool:
        return self.status is RunStatus.COMPLETED


def run_markdown_workflow(
    source: str | Path,
    *,
    output_dir: str | Path,
    headless: bool = True,
) -> WorkflowRunResult:
    """Compile, execute, sanitize, and export one Markdown workflow."""
    source_path = Path(source).resolve()
    compiled = MarkdownWorkflowSpec.load(source_path).compile()
    output_root = Path(output_dir).resolve()
    asset_path = output_root / f"{compiled.name}.json"
    pytest_path = output_root / f"test_{compiled.name}.py"
    workspace = source_path.parent
    session = RecordingSession.create(
        name=compiled.name,
        goal=compiled.parameterized_goal,
        base_url=compiled.parameterized_start_url,
    )
    input_paths: list[JsonValue] = list(sorted(_leaf_paths(compiled.data)))
    workflow_metadata: dict[str, JsonValue] = {
        "schema": "web_e2e_workflow.v1",
        "source": source_path.name,
        "input_paths": input_paths,
    }
    session.asset.metadata["workflow"] = workflow_metadata
    tools = ExternalToolRegistry(workspace, workspace / ".web-e2e" / "tools")
    device = PlaywrightDevice(
        headless=headless,
        artifact_dir=output_root / "observations",
    )
    verifier = DeterministicVerifier()
    registry = default_capability_catalog().build_action_registry(
        CapabilityContext(device=device, tools=tools)
    )
    try:
        device.start()
        navigation = Action(
            name="navigate",
            channel=ActionChannel.BROWSER,
            arguments={"url": compiled.parameterized_start_url},
        )
        tasks = PlanCompiler(registry).compile(
            PlanningResult.actions(
                (
                    PlannedAction(
                        navigation,
                        f"Open {compiled.start_url}",
                        f"The browser reaches {compiled.start_url}",
                    ),
                )
            )
        )
        initial = ExecutionRunner(
            observer=device,
            verifier=verifier,
            actions=registry,
            inputs=compiled.data,
        ).run(
            session,
            tasks,
            source=StepSource.IMPORTED,
        )
        if not initial.steps:
            raise RuntimeError(
                f"Workflow initial navigation could not be verified: {compiled.start_url}"
            )
        result = AuthoringAgent(
            observer=device,
            planner=StructuredVLMPlanner(OpenAICompatibleVLMBackend.from_env()),
            verifier=verifier,
            actions=registry,
            max_rounds=max(12, len(compiled.steps) * 4),
            inputs=compiled.data,
        ).perform(
            session,
            _instruction_with_reference_notes(compiled),
            initial_memories=compiled.memories,
        )
        _restore_secret_references(session, compiled.secret_values)
        session.asset.execution_history[:] = [
            _replace_secret_mapping(item, compiled.secret_values)
            for item in session.asset.execution_history
        ]
        _assert_secret_safe_asset(session, compiled.secret_values)
        JsonAssetStore().save(session.asset, asset_path)
        PytestExporter().export(session.asset, pytest_path)
        return WorkflowRunResult(
            status=result.status,
            asset_path=asset_path,
            output_path=pytest_path,
            summary=result.summary,
            question=result.question,
        )
    finally:
        device.close()


def _instruction_with_reference_notes(compiled: CompiledWorkflow) -> str:
    notes = compiled.reference_notes.strip()
    if not notes:
        return compiled.authoring_instruction
    return (
        f"{compiled.authoring_instruction}\n\n"
        "Business reference notes (context only; the ordered stages and live "
        f"page remain authoritative):\n{notes[:20000]}"
    )


def _leaf_paths(value: JsonValue, prefix: str = "") -> set[str]:
    if isinstance(value, dict):
        paths: set[str] = set()
        for key, item in value.items():
            child = f"{prefix}.{key}" if prefix else key
            paths.update(_leaf_paths(item, child))
        return paths
    if isinstance(value, list):
        list_paths: set[str] = set()
        for index, item in enumerate(value):
            child = f"{prefix}.{index}" if prefix else str(index)
            list_paths.update(_leaf_paths(item, child))
        return list_paths
    return {prefix} if prefix else set()


def _restore_secret_references(
    session: RecordingSession,
    secret_values: dict[str, str],
) -> None:
    if not secret_values:
        return
    session.asset.steps[:] = [
        replace(step, action=_sanitize_action(step, secret_values))
        for step in session.asset.steps
    ]


def _sanitize_action(
    step: RecordedStep,
    secret_values: dict[str, str],
) -> Action:
    arguments = _replace_secrets(step.action.arguments, secret_values)
    if not isinstance(arguments, dict):
        raise RuntimeError("Action argument sanitization changed its shape")
    return replace(
        step.action,
        arguments=arguments,
    )


def _replace_secrets(
    value: JsonValue,
    secret_values: dict[str, str],
) -> JsonValue:
    if isinstance(value, str):
        for secret, reference in sorted(
            secret_values.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if secret:
                value = value.replace(secret, reference)
        return value
    if isinstance(value, list):
        return [_replace_secrets(item, secret_values) for item in value]
    if isinstance(value, dict):
        return {
            str(key): _replace_secrets(item, secret_values)
            for key, item in value.items()
        }
    return value


def _replace_secret_text(value: str, secret_values: dict[str, str]) -> str:
    replaced = _replace_secrets(value, secret_values)
    if not isinstance(replaced, str):
        raise RuntimeError("Secret text sanitization changed its shape")
    return replaced


def _replace_secret_mapping(
    value: dict[str, JsonValue],
    secret_values: dict[str, str],
) -> dict[str, JsonValue]:
    replaced = _replace_secrets(value, secret_values)
    if not isinstance(replaced, dict):
        raise RuntimeError("Execution history sanitization changed its shape")
    return replaced


def _assert_secret_safe_asset(
    session: RecordingSession,
    secret_values: dict[str, str],
) -> None:
    import json

    content = json.dumps(session.asset.to_dict(), ensure_ascii=False)
    leaked = [secret for secret in secret_values if secret and secret in content]
    if leaked:
        raise RuntimeError("Secret value reached the persistable TestAsset")


__all__ = ["WorkflowRunResult", "run_markdown_workflow"]
