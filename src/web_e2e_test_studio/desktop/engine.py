"""UI-independent recording service used by the Desktop Studio."""

from __future__ import annotations

import inspect
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from web_e2e_test_studio.agent import (
    ActionSpec,
    AuthoringAgent,
    ExecutionRunner,
    PlanCompiler,
    PlannedAction,
    PlanningResult,
)
from web_e2e_test_studio.agent.control import (
    ActionExecutionContext,
    CancellationToken,
)
from web_e2e_test_studio.agent.lifecycle import (
    TaskFailure,
    TaskStateSnapshot,
    classify_exception,
)
from web_e2e_test_studio.agent.loop import AgentEvent, AgentRunResult, Planner
from web_e2e_test_studio.agent.planner import (
    OpenAICompatibleVLMBackend,
    StructuredVLMPlanner,
)
from web_e2e_test_studio.agent.verification import DeterministicVerifier
from web_e2e_test_studio.application import (
    _build_action_registry,
    _execute_and_record,
    _launch_external_application,
    _persist_outputs,
    _require_page,
    _run_external_action,
    _sync_manual_events,
)
from web_e2e_test_studio.browser import BrowserPageInfo, PlaywrightDevice
from web_e2e_test_studio.context import (
    ContextPurpose,
    ContextRequest,
    UIContext,
    capture_observation,
)
from web_e2e_test_studio.desktop.recovery import (
    RunCheckpoint,
    RunCheckpointStore,
)
from web_e2e_test_studio.desktop.validation import (
    ModelFreeValidationResult,
    ModelFreeValidator,
    StepValidationEvent,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    JsonValue,
    Locator,
    Observation,
    RecordedStep,
    StepSource,
    utc_now,
)
from web_e2e_test_studio.exporters import PytestExporter
from web_e2e_test_studio.files import SpreadsheetExtractor
from web_e2e_test_studio.recording import RecordingSession
from web_e2e_test_studio.recording.manual import BrowserEventRecorder
from web_e2e_test_studio.storage import JsonAssetStore
from web_e2e_test_studio.tools import ExternalToolRegistry
from web_e2e_test_studio.variables import (
    VariableDefinition,
    VariableRegistry,
    VLMVariableDefinitionCompiler,
    VLMVariableExtractionCompiler,
)


@dataclass(frozen=True)
class DesktopRecordingConfig:
    """Everything required to start one desktop recording session."""

    url: str
    goal: str
    name: str
    asset_path: Path
    output_path: Path
    workspace: Path
    tools: tuple[Path, ...] = ()
    headless: bool = False
    checkpoint_path: Path | None = None


@dataclass(frozen=True)
class DesktopRecordingSnapshot:
    """Serializable state presented by the recording engine to a UI."""

    running: bool
    steps: tuple[RecordedStep, ...]
    last_observation: Observation
    asset_path: Path
    output_path: Path
    pages: tuple[BrowserPageInfo, ...] = ()
    active_page_id: str = ""
    variables: tuple[dict[str, str], ...] = ()

    @property
    def step_count(self) -> int:
        return len(self.steps)


class DesktopRecordingEngine:
    """Own one browser session and expose coarse-grained Studio commands."""

    def __init__(
        self,
        config: DesktopRecordingConfig,
        *,
        device: Any | None = None,
        recorder: Any | None = None,
        planner_factory: Callable[[], Planner] | None = None,
        validator: ModelFreeValidator | None = None,
        variable_compiler: Callable[[str, str], VariableDefinition] | None = None,
        variable_extraction_compiler: Callable[[str, str, str, str, str], VariableDefinition]
        | None = None,
    ) -> None:
        self._config = config
        self._asset_path = config.asset_path.resolve()
        self._output_path = config.output_path.resolve()
        self._workspace = config.workspace.resolve()
        self._device = device or PlaywrightDevice(
            headless=config.headless,
            artifact_dir=self._asset_path.parent / "observations",
        )
        self._recorder = recorder or BrowserEventRecorder()
        self._planner_factory = planner_factory or _planner_from_env
        self._session = RecordingSession.create(
            name=config.name,
            goal=config.goal,
            base_url=config.url,
        )
        self._store = JsonAssetStore()
        self._checkpoint_store = RunCheckpointStore(self._workspace)
        self._checkpoint = RunCheckpoint.create(
            name=config.name,
            goal=config.goal,
            asset_path=self._asset_path,
            output_path=self._output_path,
            workspace=self._workspace,
            initial_url=config.url,
            headless=config.headless,
        )
        self._exporter = PytestExporter()
        self._verifier = DeterministicVerifier()
        self._tools = ExternalToolRegistry(
            self._workspace,
            self._workspace / ".web-e2e" / "tools",
        )
        self._running = False
        self._browser_started = False
        self._last_observation = Observation()
        self._actions = _build_action_registry(self, self._tools)
        self._active_cancellation: CancellationToken | None = None
        self._validator = validator or ModelFreeValidator()
        self._variable_compiler = variable_compiler or _compile_variable_from_env
        self._variable_extraction_compiler = (
            variable_extraction_compiler or _compile_variable_extraction_from_env
        )
        self._variables = VariableRegistry(definition_compiler=self._variable_compiler)
        self._preview_listener: Callable[[Observation, str], None] | None = None

    def start(self) -> DesktopRecordingSnapshot:
        """Launch the browser, register tools, navigate, and persist outputs."""
        if self._running:
            raise RuntimeError("The recording session is already running")
        for tool_path in self._config.tools:
            self._tools.register_path(tool_path, authorized=True)
        self._actions = _build_action_registry(self, self._tools)
        self._session.asset.tool_dependencies.update(self._tools.tool_dependencies())
        self._running = True
        try:
            if self._config.url:
                self._ensure_browser_started()
                step = _execute_and_record(
                    session=self._session,
                    action=Action(
                        name="navigate",
                        channel=ActionChannel.BROWSER,
                        arguments={"url": self._config.url},
                    ),
                    intent=f"Open {self._config.url}",
                    expected_result=f"The browser reaches {self._config.url}",
                    source=StepSource.USER,
                    device=self,
                    actions=self._actions,
                    verifier=self._verifier,
                )
                if step is None:
                    raise RuntimeError(
                        f"Initial navigation could not be verified: {self._config.url}"
                    )
            return self.save()
        except Exception:
            self.stop()
            raise

    def resume(self) -> DesktopRecordingSnapshot:
        """Restore verified state and reopen the last known browser page."""
        if self._running:
            raise RuntimeError("The recording session is already running")
        checkpoint_path = self._config.checkpoint_path or self._asset_path.with_suffix(".run.json")
        checkpoint = self._checkpoint_store.load(checkpoint_path)
        if checkpoint.asset_path != self._asset_path:
            raise ValueError("Checkpoint asset does not match this session")
        self._checkpoint = checkpoint
        self._session = RecordingSession(asset=self._store.load(self._asset_path))
        raw_definitions = self._session.asset.metadata.get("variable_definitions", [])
        self._variables = VariableRegistry(
            tuple(
                VariableDefinition.from_dict(item)
                for item in raw_definitions
                if isinstance(item, dict)
            )
            if isinstance(raw_definitions, list)
            else (),
            definition_compiler=self._variable_compiler,
        )
        self._running = True
        try:
            last_url = checkpoint.last_url or checkpoint.initial_url
            if last_url:
                self._ensure_browser_started()
                result = self._device.execute(
                    Action(
                        "navigate",
                        ActionChannel.BROWSER,
                        arguments={"url": last_url},
                    )
                )
                if not result.success:
                    raise RuntimeError(f"Could not restore browser page: {result.error}")
            self._checkpoint = self._checkpoint.updated(
                state=("waiting_for_user" if self._checkpoint.pending_question else "active")
            )
            return self.save()
        except Exception as exc:
            self.stop()
            failure = classify_exception(exc)
            self._checkpoint = self._checkpoint.updated(
                state="failed",
                last_error=str(exc),
                last_failure=failure,
            )
            self._checkpoint_store.save(self._checkpoint)
            raise

    def sync_manual(self) -> DesktopRecordingSnapshot:
        """Capture browser events performed manually since the previous sync."""
        self._require_running()
        if not self._browser_started:
            raise RuntimeError("This session has no active browser")
        _sync_manual_events(
            self._recorder,
            _require_page(self._device.page),
            self._session,
            self._device,
            self._verifier,
            self._actions,
        )
        return self.save()

    def run_ai(
        self,
        instruction: str,
        event_sink: Callable[[AgentEvent], None] | None = None,
    ) -> tuple[AgentRunResult, DesktopRecordingSnapshot]:
        """Let the configured VLM perform and verify one instruction."""
        self._require_running()
        if not instruction.strip():
            raise ValueError("The VLM instruction cannot be empty")
        response_text = instruction.strip()
        original_instruction = self._checkpoint.active_instruction or response_text
        text = str(self._variables.resolve(response_text))
        first_new_step = len(self._session.asset.steps)
        manual_steps: tuple[RecordedStep, ...] = ()
        if self._browser_started:
            manual_steps = tuple(
                _sync_manual_events(
                    self._recorder,
                    _require_page(self._device.page),
                    self._session,
                    self._device,
                    self._verifier,
                    self._actions,
                )
            )
            if manual_steps:
                self.save()
        if self._checkpoint.pending_question and self._checkpoint.active_instruction:
            text = (
                f"Continue the original request:\n{original_instruction}\n\n"
                "The agent previously asked:\n"
                f"{self._checkpoint.pending_question}\n\n"
                f"User response:\n{response_text}"
            )
        text = str(self._variables.resolve(text))
        self._checkpoint = self._checkpoint.updated(
            state="running",
            active_instruction=original_instruction,
            pending_question="",
            last_error="",
            last_failure=None,
        )
        self._persist_checkpoint()
        cancellation = CancellationToken()
        self._active_cancellation = cancellation
        deferred_done_events: list[AgentEvent] = []

        def checkpoint_event(event: AgentEvent) -> None:
            if event.kind == "done":
                if manual_steps:
                    payload = dict(event.payload)
                    payload["verified_steps"] = _event_verified_steps(payload) + len(manual_steps)
                    payload["status"] = "completed"
                    event = replace(event, payload=payload)
                deferred_done_events.append(event)
                return
            changes: dict[str, Any] = {}
            round_value = event.payload.get("round")
            if isinstance(round_value, int):
                changes["round_number"] = round_value
            url_value = event.payload.get("url")
            if isinstance(url_value, str) and url_value:
                changes["last_url"] = url_value
            question = event.payload.get("question")
            if isinstance(question, str) and question:
                changes["state"] = "waiting_for_user"
                changes["pending_question"] = question
            memory = event.payload.get("memory")
            if isinstance(memory, str) and memory.strip():
                changes["memories"] = tuple(
                    dict.fromkeys((*self._checkpoint.memories, memory.strip()))
                )[-100:]
            task_id = event.payload.get("task_id")
            lifecycle = event.payload.get("lifecycle")
            event_failure = event.payload.get("failure")
            if not task_id and isinstance(event_failure, dict):
                changes["last_failure"] = TaskFailure.from_dict(event_failure)
            if isinstance(task_id, str) and task_id and isinstance(lifecycle, dict):
                task_lifecycles = dict(self._checkpoint.task_lifecycles)
                task_lifecycles[task_id] = TaskStateSnapshot.from_dict(
                    {
                        "lifecycle": lifecycle,
                        "failure": event.payload.get("failure"),
                    }
                )
                changes["task_lifecycles"] = task_lifecycles
            self._checkpoint = self._checkpoint.with_event(
                event.kind,
                event.payload,
            ).updated(**changes)
            self._store.save(self._session.asset, self._asset_path)
            self._persist_checkpoint()
            if event_sink is not None:
                event_sink(event)

        try:
            result = AuthoringAgent(
                observer=self,
                planner=self._planner_factory(),
                verifier=self._verifier,
                actions=self._actions,
                event_sink=checkpoint_event,
                cancellation=cancellation,
            ).perform(
                self._session,
                text,
                initial_memories=self._checkpoint.memories,
            )
            if manual_steps:
                corrected_events = tuple(
                    replace(
                        event,
                        payload={
                            **event.payload,
                            "verified_steps": _event_verified_steps(event.payload)
                            + len(manual_steps),
                            "status": "completed",
                        },
                    )
                    if event.kind == "done"
                    else event
                    for event in result.events
                )
                result = replace(
                    result,
                    events=corrected_events,
                    steps=(*manual_steps, *result.steps),
                )
        except Exception as exc:
            failure = self._checkpoint.last_failure or classify_exception(exc)
            self._checkpoint = self._checkpoint.updated(
                state="failed",
                last_error=str(exc),
                last_failure=failure,
            )
            self._persist_checkpoint()
            raise
        finally:
            self._active_cancellation = None
        result_status = result.status.value
        checkpoint_state = {
            "waiting_for_user": "waiting_for_user",
            "cancelled": "cancelled",
        }.get(result_status, "active")
        self._checkpoint = self._checkpoint.updated(
            state=checkpoint_state,
            active_instruction=(
                original_instruction if result_status in {"waiting_for_user", "cancelled"} else ""
            ),
            pending_question=result.question,
        )
        self.save()
        self._restore_variable_references(first_new_step)
        snapshot = self.save()
        for event in deferred_done_events:
            self._checkpoint = self._checkpoint.with_event(event.kind, event.payload)
            self._persist_checkpoint()
            if event_sink is not None:
                event_sink(event)
        return result, snapshot

    def cancel(self) -> bool:
        """Cooperatively interrupt the currently executing Agent action."""
        cancellation = self._active_cancellation
        if cancellation is None:
            return False
        cancellation.cancel("Cancelled by user")
        return True

    def register_tool(self, path: str | Path) -> DesktopRecordingSnapshot:
        """Copy and authorize an external tool for this recording."""
        self._require_running()
        self._tools.register_path(path, authorized=True)
        self._actions = _build_action_registry(self, self._tools)
        self._session.asset.tool_dependencies.update(self._tools.tool_dependencies())
        return self.save()

    def run_tool(self, tool_ref: str, arguments: Sequence[str] = ()) -> DesktopRecordingSnapshot:
        """Execute a registered tool and record its verified result."""
        self._require_running()
        _run_external_action(
            tool_ref,
            arguments,
            session=self._session,
            device=self,
            actions=self._actions,
            verifier=self._verifier,
            tools=self._tools,
        )
        self._session.asset.tool_dependencies.update(self._tools.tool_dependencies())
        return self.save()

    def run_http_request(
        self,
        request: dict[str, Any],
    ) -> DesktopRecordingSnapshot:
        """Execute and record a user-authored HTTP request."""
        self._require_running()
        resolved_value = self._variables.resolve(request)
        if not isinstance(resolved_value, dict):
            raise TypeError("Resolved HTTP request must be an object")
        resolved_request = dict(resolved_value)
        method = str(resolved_request.get("method", "")).strip().upper()
        url = str(resolved_request.get("url", "")).strip()
        expected_status = resolved_request.get("expected_status", 200)
        attempt_count = len(self._session.attempts)
        step = _execute_and_record(
            session=self._session,
            action=Action(
                name="http_request",
                channel=ActionChannel.API,
                arguments=resolved_request,
            ),
            intent=f"Send {method} request to {url}",
            expected_result=f"The API returns HTTP {expected_status}",
            source=StepSource.USER,
            device=self,
            actions=self._actions,
            verifier=self._verifier,
        )
        if step is None:
            reason = "The request did not produce a verified result"
            if len(self._session.attempts) > attempt_count:
                attempt = self._session.attempts[-1]
                reason = attempt.result.error or attempt.verification.reason or reason
            raise RuntimeError(f"HTTP request was not recorded: {method} {url}: {reason}")
        if self._variables.referenced_names(request):
            original_action = Action(
                name="http_request",
                channel=ActionChannel.API,
                arguments=dict(request),
            )
            for index, recorded in enumerate(self._session.asset.steps):
                if recorded.id == step.id:
                    self._session.asset.steps[index] = replace(recorded, action=original_action)
                    break
        return self.save()

    def run_recorded_step(
        self, step_id: str
    ) -> tuple[dict[str, JsonValue], DesktopRecordingSnapshot]:
        """Execute one trusted persisted step without duplicating the asset step."""
        self._require_running()
        recorded = next(
            (step for step in self._session.asset.steps if step.id == step_id),
            None,
        )
        if recorded is None:
            raise ValueError(f"Recorded step not found: {step_id}")

        resolved_arguments = self._variables.resolve(recorded.action.arguments)
        if not isinstance(resolved_arguments, dict):
            raise TypeError("Resolved step arguments must be an object")
        action = replace(recorded.action, arguments=dict(resolved_arguments))
        tasks = PlanCompiler(self._actions).compile(
            PlanningResult.actions(
                (
                    PlannedAction(
                        action,
                        recorded.intent,
                        recorded.expected_result,
                    ),
                )
            )
        )
        scratch = RecordingSession.create(
            name=f"single-step-{self._session.asset.name}",
            goal=recorded.intent,
            base_url=self._session.asset.base_url,
        )
        cancellation = CancellationToken()
        self._active_cancellation = cancellation
        try:
            execution = ExecutionRunner(
                observer=self,
                verifier=self._verifier,
                actions=self._actions,
                cancellation=cancellation,
            ).run(scratch, tasks, source=StepSource.USER)
        finally:
            self._active_cancellation = None
        record = execution.records[0]
        result: dict[str, JsonValue] = {
            "step_id": recorded.id,
            "action": recorded.action.name,
            "channel": recorded.action.channel.value,
            "status": record.status.value,
            "result": {
                "status": record.result.status.value,
                "error": record.result.error,
                "error_category": record.result.error_category,
            },
            "verification": {
                "status": record.verification.status.value,
                "reason": record.verification.reason,
            },
            "failure": record.failure.to_dict() if record.failure else None,
        }
        self._session.asset.execution_history.append(
            {
                "kind": "historical_step_execution",
                "executed_at": utc_now(),
                **result,
            }
        )
        self._store.save(self._session.asset, self._asset_path)
        self._last_observation = record.after
        return result, self.snapshot()

    def define_variable(
        self, name: str, description: str
    ) -> tuple[VariableDefinition, str, DesktopRecordingSnapshot]:
        """Create a portable generator rule and a session preview value."""
        self._require_running()
        definition, value = self._variables.define(name, description)
        self._persist_variable_definitions()
        return definition, value, self.save()

    def extract_variable(
        self,
        name: str,
        description: str,
        source: str,
        file_path: str = "",
    ) -> tuple[VariableDefinition, str, DesktopRecordingSnapshot]:
        """Extract one scalar through a deterministic browser or workbook rule."""
        self._require_running()
        if source == "page":
            self._ensure_browser_started()
            observation = self.observe()
            context = json.dumps(
                {
                    "url": observation.url,
                    "title": observation.title,
                    "page_text": observation.page_text,
                    "interactive_elements": observation.interactive_elements,
                },
                ensure_ascii=False,
            )
        elif source == "spreadsheet":
            context = SpreadsheetExtractor((self._workspace, self._asset_path.parent)).context(
                file_path
            )
        else:
            raise ValueError("Variable extraction source must be page or spreadsheet")
        definition = self._variable_extraction_compiler(
            name, description, source, context, file_path
        )
        action = _variable_extraction_action(definition)
        attempt_count = len(self._session.attempts)
        step = _execute_and_record(
            session=self._session,
            action=action,
            intent=f"Extract variable {name} from {source}",
            expected_result=f"Exactly one scalar value is stored as {name}",
            source=StepSource.USER,
            device=self,
            actions=self._actions,
            verifier=self._verifier,
        )
        if step is None or len(self._session.attempts) == attempt_count:
            raise RuntimeError(f"Could not extract variable {name}")
        result = self._session.attempts[-1].result
        value = result.output.get("value")
        if not result.success or not isinstance(value, (str, int, float, bool)):
            raise RuntimeError(result.error or f"Variable {name} has no scalar value")
        stored, preview = self._variables.store(definition, str(value))
        self._persist_variable_definitions()
        return stored, preview, self.save()

    def import_and_launch_tool(
        self, path: str | Path, arguments: Sequence[str] = ()
    ) -> DesktopRecordingSnapshot:
        """Import an executable, launch it, and record one deterministic step."""
        self._require_running()
        tool = self._tools.register_path(path, authorized=True)
        if tool.kind != "exe":
            raise ValueError("Manual application launch requires an .exe file")
        self._actions = _build_action_registry(self, self._tools)
        _launch_external_application(
            tool.id,
            arguments,
            session=self._session,
            device=self,
            actions=self._actions,
            verifier=self._verifier,
            tools=self._tools,
        )
        self._session.asset.tool_dependencies.update(self._tools.tool_dependencies())
        return self.save()

    def save(self) -> DesktopRecordingSnapshot:
        """Persist the model-independent asset and regenerate plain Pytest."""
        self._require_running()
        _persist_outputs(
            self._session,
            self._store,
            self._exporter,
            self._asset_path,
            self._output_path,
        )
        self._last_observation = self.observe()
        if self._last_observation.url:
            self._checkpoint = self._checkpoint.updated(last_url=self._last_observation.url)
        self._persist_checkpoint()
        return self.snapshot()

    def validate_export(
        self,
        event_sink: Callable[[StepValidationEvent], None] | None = None,
    ) -> tuple[ModelFreeValidationResult, DesktopRecordingSnapshot]:
        """Regenerate and execute the exported test without model access."""
        self.save()
        result = self._validator.run(
            self._output_path,
            workspace=self._workspace,
            event_sink=event_sink,
        )
        validation_record: dict[str, JsonValue] = {
            "kind": "model_free_validation",
            "validated_at": utc_now(),
            "status": "passed" if result.passed else "failed",
            "exit_code": result.exit_code,
            "duration_seconds": result.duration_seconds,
            "test_path": str(result.test_path),
            "steps": [
                {
                    "step_id": step.step_id,
                    "action": step.action,
                    "status": step.status,
                }
                for step in result.steps
            ],
        }
        self._session.asset.execution_history.append(validation_record)
        self._session.asset.metadata["validation_status"] = (
            "replay_verified" if result.passed else "replay_failed"
        )
        self._session.asset.metadata["last_validation"] = validation_record
        self._store.save(self._session.asset, self._asset_path)
        return result, self.snapshot()

    def stop(self) -> DesktopRecordingSnapshot:
        """Close the owned browser and return the final state."""
        if self._running:
            if self._browser_started:
                self._device.close()
                self._browser_started = False
            self._running = False
            self._checkpoint = self._checkpoint.updated(state="suspended")
            if self._asset_path.is_file():
                self._persist_checkpoint()
        return self.snapshot()

    def preview(self) -> DesktopRecordingSnapshot:
        """Refresh only the live browser frame without persisting outputs."""
        self._require_running()
        if not self._browser_started:
            return self.snapshot()
        preview = getattr(self._device, "preview", None)
        self._last_observation = preview() if callable(preview) else self.observe()
        return self.snapshot()

    def set_preview_listener(self, listener: Callable[[Observation, str], None]) -> None:
        """Publish CDP frames independently from Desktop command polling."""
        self._preview_listener = listener
        add_listener = getattr(self._device, "add_preview_listener", None)
        if callable(add_listener):
            add_listener(self._handle_preview_frame)

    def _handle_preview_frame(self, observation: Observation, page_id: str) -> None:
        self._last_observation = observation
        if self._preview_listener is not None:
            self._preview_listener(observation, page_id)

    def activate_page(self, page_id: str) -> DesktopRecordingSnapshot:
        """Switch the active tab/popup and refresh its live frame."""
        self._require_running()
        if not self._browser_started:
            raise RuntimeError("This session has no active browser")
        activate_page = getattr(self._device, "activate_page", None)
        if not callable(activate_page):
            raise RuntimeError("The browser does not support tab switching")
        activate_page(page_id)
        return self.preview()

    def observe(self) -> Observation:
        """Return a browser snapshot, or an empty context for API/tool sessions."""
        if not self._browser_started:
            return Observation()
        return capture_observation(
            self._device,
            ContextRequest(
                ContextPurpose.PREVIEW,
                force_refresh=True,
            ),
        )

    def capture_context(self, request: ContextRequest | None = None) -> UIContext:
        """Expose the rich context seam while keeping browser startup lazy."""
        if not self._browser_started:
            return UIContext()
        capture_context = getattr(self._device, "capture_context", None)
        if callable(capture_context):
            context = capture_context(request or ContextRequest())
            if not isinstance(context, UIContext):
                raise TypeError("Browser capture_context() must return UIContext")
            return context
        observation = self._device.observe()
        return UIContext(
            url=observation.url,
            title=observation.title,
            screenshot_path=observation.screenshot_path,
            viewport=dict(observation.viewport),
            interactive_elements=observation.interactive_elements,
            page_text=observation.page_text,
        )

    def action_specs(self) -> tuple[ActionSpec, ...]:
        """Expose browser action contracts while keeping startup lazy."""
        return tuple(self._device.action_specs())

    def execute(
        self,
        action: Action,
        control: ActionExecutionContext | None = None,
    ) -> ActionResult:
        """Start the browser on the first browser action, then delegate."""
        if action.channel is not ActionChannel.BROWSER:
            return ActionResult.failed(
                f"Desktop browser executor cannot run {action.channel.value}"
            )
        try:
            self._ensure_browser_started()
        except Exception as exc:
            return ActionResult.failed(str(exc))
        parameters = inspect.signature(self._device.execute).parameters
        if control is None or len(parameters) < 2:
            return self._device.execute(action)
        return self._device.execute(action, control)

    def _ensure_browser_started(self) -> None:
        if self._browser_started:
            return
        add_page_listener = getattr(self._device, "add_page_listener", None)
        if callable(add_page_listener):
            add_page_listener(self._recorder.install)
            recorder_follows_pages = True
        else:
            recorder_follows_pages = False
        self._device.start()
        page = _require_page(self._device.page)
        if not recorder_follows_pages:
            self._recorder.install(page)
        self._browser_started = True

    def snapshot(self) -> DesktopRecordingSnapshot:
        pages_method = getattr(self._device, "pages", None)
        pages = tuple(pages_method()) if self._browser_started and callable(pages_method) else ()
        active_page_id = next(
            (page.id for page in pages if page.active),
            "",
        )
        return DesktopRecordingSnapshot(
            running=self._running,
            steps=tuple(self._session.asset.steps),
            last_observation=self._last_observation,
            asset_path=self._asset_path,
            output_path=self._output_path,
            pages=pages,
            active_page_id=active_page_id,
            variables=tuple(
                {
                    "name": definition.name,
                    "description": definition.description,
                    "value": self._variables.values()[definition.name],
                    "reference": f"${{VAR:{definition.name}}}",
                }
                for definition in self._variables.definitions()
            ),
        )

    def _persist_variable_definitions(self) -> None:
        self._session.asset.metadata["variable_definitions"] = [
            definition.to_dict() for definition in self._variables.definitions()
        ]

    def _restore_variable_references(self, first_step: int) -> None:
        for index in range(first_step, len(self._session.asset.steps)):
            step = self._session.asset.steps[index]
            restored = self._variables.restore_references(step.action.arguments)
            if isinstance(restored, dict):
                self._session.asset.steps[index] = replace(
                    step,
                    action=replace(step.action, arguments=dict(restored)),
                )

    def _require_running(self) -> None:
        if not self._running:
            raise RuntimeError("Start a recording session first")

    def _persist_checkpoint(self) -> None:
        self._checkpoint_store.save(self._checkpoint)


def _planner_from_env() -> Planner:
    return StructuredVLMPlanner(OpenAICompatibleVLMBackend.from_env())


def _event_verified_steps(payload: dict[str, JsonValue]) -> int:
    value = payload.get("verified_steps", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _compile_variable_from_env(name: str, description: str) -> VariableDefinition:
    compiler = VLMVariableDefinitionCompiler(OpenAICompatibleVLMBackend.from_env(timeout=30))
    return compiler.compile(name, description)


def _compile_variable_extraction_from_env(
    name: str,
    description: str,
    source: str,
    context: str,
    file_path: str,
) -> VariableDefinition:
    compiler = VLMVariableExtractionCompiler(OpenAICompatibleVLMBackend.from_env(timeout=30))
    return compiler.compile(
        name,
        description,
        source=source,
        context=context,
        file_path=file_path,
    )


def _variable_extraction_action(definition: VariableDefinition) -> Action:
    options = definition.options
    if definition.kind == "page_value":
        raw_locator = options.get("locator")
        if not isinstance(raw_locator, dict):
            raise ValueError("Page variable rule has no locator")
        return Action(
            name="extract_value",
            channel=ActionChannel.BROWSER,
            locator=Locator.from_dict(raw_locator),
            arguments={
                "variable_name": definition.name,
                "read": str(options.get("read", "text")),
                "attribute": str(options.get("attribute", "")),
            },
        )
    if definition.kind == "spreadsheet_value":
        return Action(
            name="extract_spreadsheet",
            channel=ActionChannel.EXTERNAL,
            arguments={
                "variable_name": definition.name,
                "file_path": str(options.get("file_path", "")),
                "query": str(options.get("query", "")),
                "sheet": str(options.get("sheet", "")),
                "cell": str(options.get("cell", "")),
            },
        )
    raise ValueError(f"Unsupported extraction definition: {definition.kind}")


__all__ = [
    "DesktopRecordingConfig",
    "DesktopRecordingEngine",
    "DesktopRecordingSnapshot",
]
