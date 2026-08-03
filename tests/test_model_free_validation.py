from __future__ import annotations

import subprocess
from pathlib import Path

from web_e2e_test_studio.desktop.validation import ModelFreeValidator


def test_model_free_validator_runs_exact_export_without_model_credentials(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    test_path = tmp_path / "test_exported.py"
    test_path.write_text("def test_exported():\n    assert True\n", encoding="utf-8")
    monkeypatch.setenv("VLM_API_KEY", "secret")
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    captured = {}

    def fake_run(command, **options):  # noqa: ANN001, ANN202
        captured["command"] = command
        captured["options"] = options
        return subprocess.CompletedProcess(command, 0, "1 passed", "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = ModelFreeValidator(timeout_seconds=12).run(
        test_path, workspace=tmp_path
    )

    assert result.passed is True
    assert result.exit_code == 0
    assert result.output == "1 passed"
    assert captured["command"][3] == str(test_path.resolve())
    options = captured["options"]
    assert "shell" not in options
    assert "VLM_API_KEY" not in options["env"]
    assert "OPENAI_API_KEY" not in options["env"]
    assert options["env"]["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] == "1"
    assert options["timeout"] == 12


def test_model_free_validator_reports_pytest_failure(
    tmp_path: Path, monkeypatch
) -> None:  # noqa: ANN001
    test_path = tmp_path / "test_exported.py"
    test_path.write_text("def test_exported():\n    assert False\n", encoding="utf-8")

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **options: subprocess.CompletedProcess(  # noqa: ARG005
            command, 1, "FAILED test_exported.py", "assert False"
        ),
    )

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
