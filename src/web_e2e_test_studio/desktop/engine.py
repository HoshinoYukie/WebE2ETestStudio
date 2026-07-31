"""UI-independent recording service used by the Desktop Studio."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from web_e2e_test_studio.agent import ActionSpec, AuthoringAgent
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
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    Observation,
    RecordedStep,
    StepSource,
)
from web_e2e_test_studio.exporters import PytestExporter
from web_e2e_test_studio.recording import RecordingSession
from web_e2e_test_studio.recording.manual import BrowserEventRecorder
from web_e2e_test_studio.storage import JsonAssetStore
from web_e2e_test_studio.tools import ExternalToolRegistry


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

    def start(self) -> DesktopRecordingSnapshot:
        """Launch the browser, register tools, navigate, and persist outputs."""
        if self._running:
            raise RuntimeError("The recording session is already running")
        for tool_path in self._config.tools:
            self._tools.register_path(tool_path, authorized=True)
        self._actions = _build_action_registry(self, self._tools)
        self._session.asset.tool_dependencies.update(
            self._tools.tool_dependencies()
        )
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
                        "Initial navigation could not be verified: "
                        f"{self._config.url}"
                    )
            return self.save()
        except Exception:
            self.stop()
            raise

    def resume(self) -> DesktopRecordingSnapshot:
        """Restore verified state and reopen the last known browser page."""
        if self._running:
            raise RuntimeError("The recording session is already running")
        checkpoint = self._checkpoint_store.load(
            self._asset_path.with_suffix(".run.json")
        )
        if checkpoint.asset_path != self._asset_path:
            raise ValueError("Checkpoint asset does not match this session")
        self._checkpoint = checkpoint
        self._session = RecordingSession(
            asset=self._store.load(self._asset_path)
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
                    raise RuntimeError(
                        f"Could not restore browser page: {result.error}"
                    )
            self._checkpoint = self._checkpoint.updated(
                state=(
                    "waiting_for_user"
                    if self._checkpoint.pending_question
                    else "active"
                )
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
        original_instruction = (
            self._checkpoint.active_instruction or response_text
        )
        text = response_text
        if (
            self._checkpoint.pending_question
            and self._checkpoint.active_instruction
        ):
            text = (
                f"Continue the original request:\n{original_instruction}\n\n"
                "The agent previously asked:\n"
                f"{self._checkpoint.pending_question}\n\n"
                f"User response:\n{response_text}"
            )
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
                changes["memories"] = tuple(dict.fromkeys(
                    (*self._checkpoint.memories, memory.strip())
                ))[-100:]
            task_id = event.payload.get("task_id")
            lifecycle = event.payload.get("lifecycle")
            event_failure = event.payload.get("failure")
            if (
                not task_id
                and isinstance(event_failure, dict)
            ):
                changes["last_failure"] = TaskFailure.from_dict(
                    event_failure
                )
            if (
                isinstance(task_id, str)
                and task_id
                and isinstance(lifecycle, dict)
            ):
                task_lifecycles = dict(
                    self._checkpoint.task_lifecycles
                )
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
        except Exception as exc:
            failure = (
                self._checkpoint.last_failure
                or classify_exception(exc)
            )
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
                original_instruction
                if result_status in {"waiting_for_user", "cancelled"}
                else ""
            ),
            pending_question=result.question,
        )
        snapshot = self.save()
        for event in deferred_done_events:
            self._checkpoint = self._checkpoint.with_event(
                event.kind, event.payload
            )
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
        self._session.asset.tool_dependencies.update(
            self._tools.tool_dependencies()
        )
        return self.save()

    def run_tool(
        self, tool_ref: str, arguments: Sequence[str] = ()
    ) -> DesktopRecordingSnapshot:
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
        self._session.asset.tool_dependencies.update(
            self._tools.tool_dependencies()
        )
        return self.save()

    def run_http_request(
        self,
        request: dict[str, Any],
    ) -> DesktopRecordingSnapshot:
        """Execute and record a user-authored HTTP request."""
        self._require_running()
        method = str(request.get("method", "")).strip().upper()
        url = str(request.get("url", "")).strip()
        expected_status = request.get("expected_status", 200)
        attempt_count = len(self._session.attempts)
        step = _execute_and_record(
            session=self._session,
            action=Action(
                name="http_request",
                channel=ActionChannel.API,
                arguments=dict(request),
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
                reason = (
                    attempt.result.error
                    or attempt.verification.reason
                    or reason
                )
            raise RuntimeError(
                f"HTTP request was not recorded: {method} {url}: {reason}"
            )
        return self.save()

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
        self._session.asset.tool_dependencies.update(
            self._tools.tool_dependencies()
        )
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
            self._checkpoint = self._checkpoint.updated(
                last_url=self._last_observation.url
            )
        self._persist_checkpoint()
        return self.snapshot()

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
        self._last_observation = (
            preview() if callable(preview) else self.observe()
        )
        return self.snapshot()

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

    def capture_context(
        self, request: ContextRequest | None = None
    ) -> UIContext:
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
        pages = (
            tuple(pages_method())
            if self._browser_started and callable(pages_method)
            else ()
        )
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
        )

    def _require_running(self) -> None:
        if not self._running:
            raise RuntimeError("Start a recording session first")

    def _persist_checkpoint(self) -> None:
        self._checkpoint_store.save(self._checkpoint)


def _planner_from_env() -> Planner:
    return StructuredVLMPlanner(OpenAICompatibleVLMBackend.from_env())


__all__ = [
    "DesktopRecordingConfig",
    "DesktopRecordingEngine",
    "DesktopRecordingSnapshot",
]
