"""Threaded command bridge between Tkinter and the recording engine."""

from __future__ import annotations

import queue
import re
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from web_e2e_test_studio.agent.loop import AgentEvent, AgentRunResult
from web_e2e_test_studio.desktop.engine import (
    DesktopRecordingConfig,
    DesktopRecordingEngine,
    DesktopRecordingSnapshot,
)
from web_e2e_test_studio.desktop.validation import (
    ModelFreeValidationResult,
    StepValidationEvent,
)
from web_e2e_test_studio.domain import JsonValue, Observation
from web_e2e_test_studio.variables import VariableDefinition


class RecordingEngine(Protocol):
    def start(self) -> DesktopRecordingSnapshot: ...

    def resume(self) -> DesktopRecordingSnapshot: ...

    def sync_manual(self) -> DesktopRecordingSnapshot: ...

    def preview(self) -> DesktopRecordingSnapshot: ...

    def activate_page(self, page_id: str) -> DesktopRecordingSnapshot: ...

    def set_preview_listener(
        self, listener: Callable[[Observation, str], None]
    ) -> None: ...

    def run_ai(
        self,
        instruction: str,
        event_sink: Callable[[AgentEvent], None] | None = None,
    ) -> tuple[AgentRunResult, DesktopRecordingSnapshot]: ...

    def register_tool(self, path: str | Path) -> DesktopRecordingSnapshot: ...

    def run_tool(
        self, tool_ref: str, arguments: Sequence[str] = ()
    ) -> DesktopRecordingSnapshot: ...

    def run_http_request(
        self, request: dict[str, Any]
    ) -> DesktopRecordingSnapshot: ...

    def import_and_launch_tool(
        self, path: str | Path, arguments: Sequence[str] = ()
    ) -> DesktopRecordingSnapshot: ...

    def save(self) -> DesktopRecordingSnapshot: ...

    def validate_export(
        self,
        event_sink: Callable[[StepValidationEvent], None] | None = None,
    ) -> tuple[ModelFreeValidationResult, DesktopRecordingSnapshot]: ...

    def define_variable(
        self, name: str, description: str
    ) -> tuple[VariableDefinition, str, DesktopRecordingSnapshot]: ...

    def stop(self) -> DesktopRecordingSnapshot: ...

    def cancel(self) -> bool: ...


@dataclass(frozen=True)
class StudioEvent:
    """One worker result safe for consumption on Tkinter's main thread."""

    kind: str
    message: str
    snapshot: DesktopRecordingSnapshot | None = None
    details: tuple[str, ...] = ()
    payload: dict[str, JsonValue] = field(default_factory=dict)


@dataclass(frozen=True)
class _Command:
    kind: str
    config: DesktopRecordingConfig | None = None
    value: str = ""
    arguments: tuple[str, ...] = field(default_factory=tuple)
    payload: dict[str, Any] = field(default_factory=dict)


class DesktopStudioController:
    """Serialize all Playwright operations onto a dedicated worker thread."""

    def __init__(
        self,
        *,
        engine_factory: Callable[
            [DesktopRecordingConfig], RecordingEngine
        ] = DesktopRecordingEngine,
        preview_interval: float = 0.4,
    ) -> None:
        if preview_interval <= 0:
            raise ValueError("preview_interval must be positive")
        self._engine_factory = engine_factory
        self._preview_interval = preview_interval
        self._commands: queue.Queue[_Command] = queue.Queue()
        self._events: queue.Queue[StudioEvent] = queue.Queue()
        self._latest_preview: StudioEvent | None = None
        self._latest_preview_lock = threading.Lock()
        self._engine: RecordingEngine | None = None
        self._closed = False
        self._thread = threading.Thread(
            target=self._work,
            name="web-e2e-desktop-worker",
            daemon=True,
        )
        self._thread.start()

    def start(self, config: DesktopRecordingConfig) -> None:
        self._submit(_Command("start", config=config))

    def resume(self, config: DesktopRecordingConfig) -> None:
        self._submit(_Command("resume", config=config))

    def sync_manual(self) -> None:
        self._submit(_Command("sync_manual"))

    def activate_page(self, page_id: str) -> None:
        self._submit(_Command("activate_page", value=page_id))

    def run_ai(self, instruction: str) -> None:
        self._submit(_Command("run_ai", value=instruction))

    def cancel(self) -> bool:
        """Signal cancellation directly while the worker executes an action."""
        engine = self._engine
        return engine.cancel() if engine is not None else False

    def register_tool(self, path: str | Path) -> None:
        self._submit(_Command("register_tool", value=str(path)))

    def run_tool(self, tool_ref: str, arguments: Sequence[str] = ()) -> None:
        self._submit(
            _Command("run_tool", value=tool_ref, arguments=tuple(arguments))
        )

    def run_http_request(self, request: dict[str, Any]) -> None:
        method = str(request.get("method", "")).upper()
        url = str(request.get("url", ""))
        self._events.put(
            StudioEvent(
                kind="progress",
                message=f"Sending HTTP request: {method} {url}",
            )
        )
        self._submit(_Command("run_http_request", payload=dict(request)))

    def import_and_launch_tool(
        self, path: str | Path, arguments: Sequence[str] = ()
    ) -> None:
        self._events.put(
            StudioEvent(
                kind="progress",
                message=f"Launching application: {path}",
            )
        )
        self._submit(
            _Command(
                "import_and_launch_tool",
                value=str(path),
                arguments=tuple(arguments),
            )
        )

    def save(self) -> None:
        self._submit(_Command("save"))

    def validate_export(self) -> None:
        self._events.put(
            StudioEvent(kind="progress", message="Running model-free validation")
        )
        self._submit(_Command("validate_export"))

    def define_variable(self, name: str, description: str) -> None:
        self._submit(
            _Command(
                "define_variable",
                value=name,
                payload={"description": description},
            )
        )

    def stop(self) -> None:
        self._submit(_Command("stop"))

    def poll_events(self) -> list[StudioEvent]:
        events: list[StudioEvent] = []
        while True:
            try:
                events.append(self._events.get_nowait())
            except queue.Empty:
                break
        with self._latest_preview_lock:
            preview = self._latest_preview
            self._latest_preview = None
        if preview is not None:
            events.append(preview)
        return events

    def close(self) -> None:
        if self._closed:
            return
        self._commands.put(_Command("shutdown"))
        self._thread.join(timeout=5)
        self._closed = True

    def _submit(self, command: _Command) -> None:
        if self._closed:
            raise RuntimeError("The Desktop Studio controller is closed")
        self._commands.put(command)

    def _work(self) -> None:
        while True:
            try:
                command = self._commands.get(timeout=self._preview_interval)
            except queue.Empty:
                if self._engine is None:
                    continue
                command = _Command("preview")
            if command.kind == "shutdown":
                if self._engine is not None:
                    with suppress(Exception):
                        self._engine.stop()
                return
            try:
                self._execute(command)
            except Exception as exc:
                if command.kind == "preview":
                    continue
                self._events.put(
                    StudioEvent(
                        kind="error",
                        message=format_studio_error(exc),
                        payload={"command": command.kind},
                    )
                )

    def _execute(self, command: _Command) -> None:
        if command.kind in {"start", "resume"}:
            if command.config is None:
                raise ValueError(
                    f"{command.kind.title()} requires a recording configuration"
                )
            if self._engine is not None:
                self._engine.stop()
            self._engine = self._engine_factory(command.config)
            set_preview_listener = getattr(
                self._engine, "set_preview_listener", None
            )
            if callable(set_preview_listener):
                set_preview_listener(self._emit_stream_frame)
            snapshot = (
                self._engine.resume()
                if command.kind == "resume"
                else self._engine.start()
            )
            self._emit(
                "Recording resumed"
                if command.kind == "resume"
                else "Recording started",
                snapshot,
            )
            return

        engine = self._require_engine()
        if command.kind == "preview":
            self._emit(
                "Live browser frame",
                engine.preview(),
                kind="browser_preview",
            )
        elif command.kind == "activate_page":
            self._emit(
                "Browser tab activated",
                engine.activate_page(command.value),
                kind="browser_preview",
            )
        elif command.kind == "sync_manual":
            self._emit(
                "Manual browser actions synchronized",
                engine.sync_manual(),
            )
        elif command.kind == "run_ai":
            result, snapshot = engine.run_ai(
                command.value,
                event_sink=self._emit_agent_event,
            )
            self._emit(
                f"VLM run finished: {result.status.value}",
                snapshot,
            )
        elif command.kind == "register_tool":
            self._emit(
                f"External tool registered: {command.value}",
                engine.register_tool(command.value),
            )
        elif command.kind == "run_tool":
            self._emit(
                f"External tool finished: {command.value}",
                engine.run_tool(command.value, command.arguments),
            )
        elif command.kind == "run_http_request":
            snapshot = engine.run_http_request(command.payload)
            output = snapshot.steps[-1].verification.evidence.get(
                "output", {}
            )
            self._emit(
                "HTTP request completed and recorded",
                snapshot,
                details=(str(output),),
            )
        elif command.kind == "import_and_launch_tool":
            self._emit(
                f"Application imported, launched, and recorded: {command.value}",
                engine.import_and_launch_tool(
                    command.value, command.arguments
                ),
            )
        elif command.kind == "save":
            self._emit("Asset and Pytest file saved", engine.save())
        elif command.kind == "validate_export":
            validation_result, snapshot = engine.validate_export(
                self._emit_validation_step
            )
            self._emit_validation_result(validation_result, snapshot)
        elif command.kind == "define_variable":
            description = str(command.payload.get("description", ""))
            definition, value, snapshot = engine.define_variable(
                command.value, description
            )
            self._events.put(
                StudioEvent(
                    kind="variable_defined",
                    message=f"Generated variable: {definition.name}",
                    snapshot=snapshot,
                    payload={
                        "name": definition.name,
                        "description": definition.description,
                        "reference": f"${{VAR:{definition.name}}}",
                        "value": value,
                    },
                )
            )
        elif command.kind == "stop":
            self._emit("Recording stopped", engine.stop())
            self._engine = None
        else:
            raise ValueError(f"Unknown Desktop Studio command: {command.kind}")

    def _emit_agent_event(self, event: AgentEvent) -> None:
        self._events.put(
            StudioEvent(
                kind="agent_event",
                message=event.kind.replace("_", " ").title(),
                payload=event.payload,
            )
        )

    def _emit_validation_result(
        self,
        result: ModelFreeValidationResult,
        snapshot: DesktopRecordingSnapshot,
    ) -> None:
        self._events.put(
            StudioEvent(
                kind="validation_passed" if result.passed else "validation_failed",
                message=(
                    "Model-free validation passed"
                    if result.passed
                    else "Model-free validation failed"
                ),
                snapshot=snapshot,
                payload={
                    "status": "passed" if result.passed else "failed",
                    "exit_code": result.exit_code,
                    "duration_seconds": round(result.duration_seconds, 2),
                    "test_path": str(result.test_path),
                    "output": result.output,
                },
            )
        )

    def _emit_validation_step(self, event: StepValidationEvent) -> None:
        self._events.put(
            StudioEvent(
                kind="validation_step",
                message=f"Validation step {event.status}: {event.action}",
                payload={
                    "step_id": event.step_id,
                    "action": event.action,
                    "status": event.status,
                },
            )
        )

    def _emit_stream_frame(
        self, observation: Observation, page_id: str
    ) -> None:
        event = StudioEvent(
            kind="browser_preview",
            message="Live browser frame",
            payload={
                "page_id": page_id,
                "url": observation.url,
                "title": observation.title,
                "screenshot_path": observation.screenshot_path,
            },
        )
        with self._latest_preview_lock:
            self._latest_preview = event

    def _require_engine(self) -> RecordingEngine:
        if self._engine is None:
            raise RuntimeError("Start a recording session first")
        return self._engine

    def _emit(
        self,
        message: str,
        snapshot: DesktopRecordingSnapshot,
        details: tuple[str, ...] = (),
        *,
        kind: str = "snapshot",
    ) -> None:
        event = StudioEvent(
            kind=kind,
            message=message,
            snapshot=snapshot,
            details=details,
        )
        if kind == "browser_preview":
            with self._latest_preview_lock:
                self._latest_preview = event
            return
        self._events.put(event)


def format_studio_error(error: Exception) -> str:
    """Keep provider HTML error pages out of the Desktop event timeline."""
    name = type(error).__name__
    raw = str(error).strip()
    if name == "PermissionDeniedError":
        return (
            "VLM request was denied. Check the model permission, Base URL, "
            "and the 'Ignore system proxy' setting."
        )
    if "<html" in raw.lower() or "<!--" in raw:
        clean = re.sub(r"<[^>]+>", " ", raw)
        clean = re.sub(r"\s+", " ", clean).strip()
        return f"{name}: {clean[:300]}"
    return f"{name}: {raw[:500]}"


__all__ = [
    "DesktopStudioController",
    "StudioEvent",
    "format_studio_error",
]
