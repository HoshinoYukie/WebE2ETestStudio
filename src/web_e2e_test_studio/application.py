"""CLI composition root for hybrid manual, VLM, and external-tool recording."""

from __future__ import annotations

import shlex
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from web_e2e_test_studio.agent import (
    ActionRegistry,
    AuthoringAgent,
    ExecutionRunner,
    PlanCompiler,
    PlannedAction,
    PlanningResult,
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
    ActionResult,
    JsonValue,
    Observation,
    RecordedStep,
    StepSource,
)
from web_e2e_test_studio.exporters import PytestExporter
from web_e2e_test_studio.recording import RecordingSession
from web_e2e_test_studio.recording.manual import BrowserEventRecorder
from web_e2e_test_studio.storage import JsonAssetStore
from web_e2e_test_studio.tools import ExternalToolRegistry


def run_interactive_recording(
    *,
    url: str,
    goal: str,
    name: str,
    asset_path: str | Path,
    output_path: str | Path,
    workspace: str | Path,
    tools: Sequence[str | Path] = (),
    headless: bool = False,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Run the interactive recording shell used by the ``record`` command."""
    workspace_path = Path(workspace).resolve()
    asset_target = Path(asset_path).resolve()
    output_target = Path(output_path).resolve()
    session = RecordingSession.create(name=name, goal=goal, base_url=url)
    store = JsonAssetStore()
    exporter = PytestExporter()
    verifier = DeterministicVerifier()
    tool_registry = ExternalToolRegistry(
        workspace_path,
        workspace_path / ".web-e2e" / "tools",
    )
    for tool_path in tools:
        registered = tool_registry.register_path(tool_path, authorized=True)
        output_fn(f"Registered external tool {registered.id}: {registered.name}")
    session.asset.tool_dependencies.update(tool_registry.tool_dependencies())

    device = PlaywrightDevice(
        headless=headless,
        artifact_dir=asset_target.parent / "observations",
    )
    action_registry = _build_action_registry(device, tool_registry)
    recorder = BrowserEventRecorder()

    try:
        device.start()
        page = _require_page(device.page)
        recorder.install(page)
        initial = _execute_and_record(
            session=session,
            action=Action(
                name="navigate",
                channel=ActionChannel.BROWSER,
                arguments={"url": url},
            ),
            intent=f"Open {url}",
            expected_result=f"The browser reaches {url}",
            source=StepSource.USER,
            device=device,
            actions=action_registry,
            verifier=verifier,
        )
        if initial is None:
            raise RuntimeError(f"Initial navigation could not be verified: {url}")
        _persist_outputs(session, store, exporter, asset_target, output_target)
        output_fn(_recording_help())

        while True:
            try:
                command = input_fn("web-e2e> ").strip()
            except (EOFError, KeyboardInterrupt):
                output_fn("")
                command = ":quit"

            if command in {"", ":sync"}:
                manual_steps = _sync_manual_events(
                    recorder,
                    page,
                    session,
                    device,
                    verifier,
                    action_registry,
                )
                _persist_outputs(
                    session, store, exporter, asset_target, output_target
                )
                output_fn(f"Recorded {len(manual_steps)} manual action(s).")
                continue

            if command in {":help", "help"}:
                output_fn(_recording_help())
                continue

            if command in {":save", "save"}:
                _persist_outputs(
                    session, store, exporter, asset_target, output_target
                )
                output_fn(f"Saved {asset_target} and {output_target}")
                continue

            if command.startswith(":ai "):
                instruction = command.removeprefix(":ai ").strip()
                if not instruction:
                    output_fn("Usage: :ai <instruction>")
                    continue
                try:
                    planner = StructuredVLMPlanner(
                        OpenAICompatibleVLMBackend.from_env()
                    )
                    result = AuthoringAgent(
                        observer=device,
                        planner=planner,
                        verifier=verifier,
                        actions=action_registry,
                    ).perform(session, instruction)
                    output_fn(
                        f"Agent status: {result.status.value}; "
                        f"verified steps: {len(result.steps)}"
                    )
                    if result.question:
                        output_fn(f"Question: {result.question}")
                    if result.summary:
                        output_fn(result.summary)
                except ValueError as exc:
                    output_fn(f"VLM is not configured: {exc}")
                _persist_outputs(
                    session, store, exporter, asset_target, output_target
                )
                continue

            if command.startswith(":tool "):
                external_step = _run_external_command(
                    command.removeprefix(":tool "),
                    session=session,
                    device=device,
                    actions=action_registry,
                    verifier=verifier,
                    tools=tool_registry,
                )
                session.asset.tool_dependencies.update(
                    tool_registry.tool_dependencies()
                )
                _persist_outputs(
                    session, store, exporter, asset_target, output_target
                )
                if external_step is None:
                    output_fn("External tool failed verification; see the asset trace.")
                else:
                    output_fn(f"Recorded external tool step {external_step.id}.")
                continue

            if command in {":quit", ":q", "quit", "exit"}:
                _sync_manual_events(
                    recorder,
                    page,
                    session,
                    device,
                    verifier,
                    action_registry,
                )
                _persist_outputs(
                    session, store, exporter, asset_target, output_target
                )
                output_fn(
                    f"Saved {len(session.asset.steps)} verified step(s) to "
                    f"{asset_target}; pytest: {output_target}"
                )
                return 0

            output_fn("Unknown command. Use :help for available commands.")
    finally:
        device.close()


def _build_action_registry(
    device: Any,
    tools: ExternalToolRegistry,
) -> ActionRegistry:
    return default_capability_catalog().build_action_registry(
        CapabilityContext(device=device, tools=tools)
    )


def _sync_manual_events(
    recorder: BrowserEventRecorder,
    page: Any,
    session: RecordingSession,
    device: PlaywrightDevice,
    verifier: DeterministicVerifier,
    registry: ActionRegistry,
) -> list[RecordedStep]:
    drain_all = getattr(recorder, "drain_all", None)
    captured_actions = (
        drain_all() if callable(drain_all) else recorder.drain(page)
    )
    recorded: list[RecordedStep] = []
    runner = ExecutionRunner(
        observer=device,
        verifier=verifier,
        actions=registry,
    )
    for action in captured_actions:
        after = device.observe()
        tasks = PlanCompiler(registry).compile(
            PlanningResult.actions(
                (
                    PlannedAction(
                        action,
                        _manual_intent(action),
                        _expected_result(action),
                    ),
                )
            )
        )
        result = ActionResult.succeeded({"captured_browser_event": True})
        execution = runner.ingest(
            session,
            tasks[0],
            result,
            before=Observation(),
            after=after,
            source=StepSource.MANUAL,
        )
        recorded.extend(execution.steps)
    return recorded


def _execute_and_record(
    *,
    session: RecordingSession,
    action: Action,
    intent: str,
    expected_result: str,
    source: StepSource,
    device: Any,
    actions: ActionRegistry,
    verifier: DeterministicVerifier,
) -> RecordedStep | None:
    tasks = PlanCompiler(actions).compile(
        PlanningResult.actions(
            (PlannedAction(action, intent, expected_result),)
        )
    )
    execution = ExecutionRunner(
        observer=device,
        verifier=verifier,
        actions=actions,
    ).run(session, tasks, source=source)
    return execution.steps[0] if execution.steps else None


def _run_external_command(
    command: str,
    *,
    session: RecordingSession,
    device: Any,
    actions: ActionRegistry,
    verifier: DeterministicVerifier,
    tools: ExternalToolRegistry,
) -> RecordedStep | None:
    parts = shlex.split(command, posix=False)
    if not parts:
        raise ValueError("Usage: :tool <tool-id-or-name> [arguments...]")
    requested, *arguments = parts
    return _run_external_action(
        requested,
        arguments,
        session=session,
        device=device,
        actions=actions,
        verifier=verifier,
        tools=tools,
    )


def _run_external_action(
    requested: str,
    arguments: Sequence[str],
    *,
    session: RecordingSession,
    device: Any,
    actions: ActionRegistry,
    verifier: DeterministicVerifier,
    tools: ExternalToolRegistry,
) -> RecordedStep | None:
    tool = tools.get(requested)
    if tool is None:
        matches = [item for item in tools.list_tools() if item.name == requested]
        if len(matches) != 1:
            raise ValueError(f"External tool not found: {requested}")
        tool = matches[0]
    argument_values: list[JsonValue] = list(arguments)
    action = Action(
        name="run_external_tool",
        channel=ActionChannel.EXTERNAL,
        tool_ref=tool.id,
        arguments={"args": argument_values},
    )
    return _execute_and_record(
        session=session,
        action=action,
        intent=f"Run external tool {tool.name}",
        expected_result=f"{tool.name} exits successfully",
        source=StepSource.USER,
        device=device,
        actions=actions,
        verifier=verifier,
    )


def _launch_external_application(
    requested: str,
    arguments: Sequence[str],
    *,
    session: RecordingSession,
    device: Any,
    actions: ActionRegistry,
    verifier: DeterministicVerifier,
    tools: ExternalToolRegistry,
) -> RecordedStep | None:
    tool = tools.get(requested)
    if tool is None:
        matches = [item for item in tools.list_tools() if item.name == requested]
        if len(matches) != 1:
            raise ValueError(f"External tool not found: {requested}")
        tool = matches[0]
    if tool.kind != "exe":
        raise ValueError("Only registered executable tools can be launched")
    action = Action(
        name="launch_application",
        channel=ActionChannel.EXTERNAL,
        tool_ref=tool.id,
        arguments={"args": list(arguments)},
    )
    return _execute_and_record(
        session=session,
        action=action,
        intent=f"Launch application {tool.name}",
        expected_result=f"{tool.name} starts successfully",
        source=StepSource.USER,
        device=device,
        actions=actions,
        verifier=verifier,
    )


def _persist_outputs(
    session: RecordingSession,
    store: JsonAssetStore,
    exporter: PytestExporter,
    asset_path: Path,
    output_path: Path,
) -> None:
    store.save(session.asset, asset_path)
    exporter.export(session.asset, output_path)


def _manual_intent(action: Action) -> str:
    target = ""
    if action.locator is not None:
        target = action.locator.name or action.locator.value
    if action.name == "fill":
        return f"Enter a value in {target or 'the field'}"
    if action.name == "navigate":
        return f"Navigate to {action.arguments.get('url', '')}"
    return f"{action.name.replace('_', ' ').title()} {target}".strip()


def _expected_result(action: Action) -> str:
    if action.name == "navigate":
        return f"The browser reaches {action.arguments.get('url', '')}"
    if action.name == "fill":
        return "The field contains the recorded value"
    if action.name == "select":
        return "The recorded option is selected"
    if action.name == "check":
        return "The control has the recorded checked state"
    return "The browser accepts the recorded action"


def _require_page(page: Any | None) -> Any:
    if page is None:
        raise RuntimeError("Playwright browser did not create a page")
    return page


def _recording_help() -> str:
    return """Recording started.

Interact with the opened browser, then return here:
  <Enter> / :sync       capture pending manual browser events
  :ai <instruction>     let the configured VLM plan and execute verified steps
  :tool <id> [args...]  execute and record a registered external tool
  :save                 save the asset and regenerate plain pytest
  :help                 show this help
  :quit                 sync, save, export, and close the browser

VLM configuration: VLM_API_KEY, optional VLM_BASE_URL and VLM_MODEL."""


__all__ = ["run_interactive_recording"]
