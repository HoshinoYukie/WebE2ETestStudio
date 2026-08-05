from __future__ import annotations

import subprocess
from pathlib import Path

from web_e2e_test_studio.desktop.validation import (
    ModelFreeValidator,
    StepValidationEvent,
)


def test_model_free_validator_runs_exact_export_without_model_credentials(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    test_path = tmp_path / "test_exported.py"
    test_path.write_text("def test_exported():\n    assert True\n", encoding="utf-8")
    monkeypatch.setenv("VLM_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    captured = {}

    class _Process:
        stdout = iter(["1 passed\n"])

        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            return 0

        def kill(self) -> None:
            return None

    def fake_popen(command, **options):  # noqa: ANN001, ANN202
        captured["command"] = command
        captured["options"] = options
        return _Process()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    result = ModelFreeValidator(timeout_seconds=12).run(
        test_path, workspace=tmp_path
    )

    assert result.passed is True
    assert result.exit_code == 0
    assert result.output == "1 passed"
    assert str(test_path.resolve()) in captured["command"]
    options = captured["options"]
    assert "shell" not in options
    assert "VLM_API_KEY" not in options["env"]
    assert "OPENAI_API_KEY" not in options["env"]
    assert options["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"


def test_model_free_validator_reports_pytest_failure(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    test_path = tmp_path / "test_exported.py"
    test_path.write_text("def test_exported():\n    assert False\n", encoding="utf-8")

    class _FailedProcess:
        stdout = iter(["FAILED test_exported.py\n", "assert False\n"])

        def wait(self, timeout=None):  # noqa: ANN001, ANN201
            return 1

        def kill(self) -> None:
            return None

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: _FailedProcess())

    result = ModelFreeValidator().run(test_path, workspace=tmp_path)

    assert result.passed is False
    assert result.exit_code == 1
    assert "FAILED test_exported.py" in result.output
    assert "assert False" in result.output


def test_model_free_validator_executes_a_real_standalone_pytest(
    tmp_path: Path,
) -> None:
    test_path = tmp_path / "test_standalone.py"
    test_path.write_text(
        "def test_standalone():\n    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )

    result = ModelFreeValidator(timeout_seconds=30).run(
        test_path, workspace=tmp_path
    )

    assert result.passed is True
    assert result.exit_code == 0
    assert "1 passed" in result.output


def test_model_free_validator_streams_and_collects_step_events(
    tmp_path: Path,
) -> None:
    test_path = tmp_path / "test_steps.py"
    test_path.write_text(
        "def test_steps():\n"
        "    print('__WEB_E2E_STEP__|running|step_1|navigate', flush=True)\n"
        "    print('__WEB_E2E_STEP__|passed|step_1|navigate', flush=True)\n",
        encoding="utf-8",
    )
    events = []

    result = ModelFreeValidator(timeout_seconds=30).run(
        test_path,
        workspace=tmp_path,
        event_sink=events.append,
    )

    assert [event.status for event in events] == ["running", "passed"]
    assert result.steps == (
        StepValidationEvent("step_1", "navigate", "passed"),
    )
    assert "__WEB_E2E_STEP__" not in result.output
