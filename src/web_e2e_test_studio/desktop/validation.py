"""Model-free validation of exported PyTest assets."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ModelFreeValidationResult:
    """Serializable outcome of one isolated exported-test execution."""

    passed: bool
    exit_code: int
    duration_seconds: float
    output: str
    test_path: Path


class ModelFreeValidator:
    """Run generated PyTest without exposing any configured model credentials."""

    def __init__(self, *, timeout_seconds: float = 300.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("Validation timeout must be positive")
        self._timeout_seconds = timeout_seconds

    def run(
        self, test_path: str | Path, *, workspace: str | Path
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
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(basetemp),
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._timeout_seconds,
                check=False,
            )
            output = "\n".join(
                part.strip()
                for part in (completed.stdout, completed.stderr)
                if part.strip()
            )
            return ModelFreeValidationResult(
                passed=completed.returncode == 0,
                exit_code=completed.returncode,
                duration_seconds=time.monotonic() - started,
                output=output[-12_000:],
                test_path=target,
            )
        except subprocess.TimeoutExpired as exc:
            output = "\n".join(
                str(part).strip()
                for part in (exc.stdout, exc.stderr)
                if part
            )
            return ModelFreeValidationResult(
                passed=False,
                exit_code=124,
                duration_seconds=time.monotonic() - started,
                output=(output + "\nValidation timed out").strip()[-12_000:],
                test_path=target,
            )


__all__ = ["ModelFreeValidationResult", "ModelFreeValidator"]
