"""Model-free validation of exported PyTest assets."""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_STEP_PREFIX = "__WEB_E2E_STEP__|"


@dataclass(frozen=True)
class StepValidationEvent:
    step_id: str
    action: str
    status: str


@dataclass(frozen=True)
class ModelFreeValidationResult:
    """Serializable outcome of one isolated exported-test execution."""

    passed: bool
    exit_code: int
    duration_seconds: float
    output: str
    test_path: Path
    steps: tuple[StepValidationEvent, ...] = ()


class ModelFreeValidator:
    """Run generated PyTest without exposing any configured model credentials."""

    def __init__(self, *, timeout_seconds: float = 300.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Validation timeout must be positive")
        self._timeout_seconds = timeout_seconds

    def run(
        self,
        test_path: str | Path,
        *,
        workspace: str | Path,
        event_sink: Callable[[StepValidationEvent], None] | None = None,
    ) -> ModelFreeValidationResult:
        target = Path(test_path).resolve()
        root = Path(workspace).resolve()
        if not target.is_file():
            raise FileNotFoundError(f"Exported PyTest file not found: {target}")
        validation_root = root / ".web-e2e" / "validation"
        validation_root.mkdir(parents=True, exist_ok=True)
        basetemp = validation_root / uuid.uuid4().hex
        environment = dict(os.environ)
        for name in (
            "VLM_API_KEY",
            "VLM_BASE_URL",
            "VLM_MODEL",
            "OPENAI_API_KEY",
            "OPENAI_BASE_URL",
        ):
            environment.pop(name, None)
        environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
        command = [
            sys.executable,
            "-m",
            "pytest",
            str(target),
            "-q",
            "-s",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(basetemp),
        ]
        started = time.monotonic()
        process = subprocess.Popen(
                command,
                cwd=root,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        lines: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                lines.put(line.rstrip("\r\n"))
            lines.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        output_lines: list[str] = []
        latest_steps: dict[str, StepValidationEvent] = {}
        timed_out = False
        deadline = started + self._timeout_seconds
        finished_output = False
        while not finished_output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                process.kill()
                break
            try:
                line = lines.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if line is None:
                finished_output = True
                continue
            event = _parse_step_event(line)
            if event is None:
                output_lines.append(line)
                continue
            latest_steps[event.step_id] = event
            if event_sink is not None:
                event_sink(event)
        exit_code = process.wait(timeout=5)
        reader.join(timeout=1)
        if timed_out:
            exit_code = 124
            output_lines.append("Validation timed out")
            for step_id, event in tuple(latest_steps.items()):
                if event.status == "running":
                    timeout_event = StepValidationEvent(
                        step_id, event.action, "timeout"
                    )
                    latest_steps[step_id] = timeout_event
                    if event_sink is not None:
                        event_sink(timeout_event)
        return ModelFreeValidationResult(
            passed=exit_code == 0,
            exit_code=exit_code,
            duration_seconds=time.monotonic() - started,
            output="\n".join(output_lines).strip()[-12_000:],
            test_path=target,
            steps=tuple(latest_steps.values()),
        )


def _parse_step_event(line: str) -> StepValidationEvent | None:
    if not line.startswith(_STEP_PREFIX):
        return None
    parts = line.split("|", 3)
    if len(parts) != 4 or parts[1] not in {
        "running",
        "passed",
        "failed",
        "timeout",
    }:
        return None
    return StepValidationEvent(
        status=parts[1], step_id=parts[2], action=parts[3]
    )


__all__ = [
    "ModelFreeValidationResult",
    "ModelFreeValidator",
    "StepValidationEvent",
]
