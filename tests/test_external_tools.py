import json
import sys
import threading
import time
from pathlib import Path

import pytest

from web_e2e_test_studio.agent.control import (
    ActionExecutionContext,
    CancellationToken,
)
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    ActionStatus,
)
from web_e2e_test_studio.tools import ExternalToolExecutor, ExternalToolRegistry


def _write_python(path: Path, source: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def test_registry_describes_python_tool_and_builds_portable_dependency(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    entry = _write_python(workspace / "helpers" / "echo.py", "print('ok')\n")
    registry = ExternalToolRegistry(workspace, workspace / ".studio" / "tools")

    tool = registry.register_path(
        entry,
        name="Echo helper",
        default_args=("--format", "json"),
        default_env={"TOOL_MODE": "record"},
        timeout=12.5,
    )
    same_tool = registry.register_path(entry, name="Echo helper")

    assert same_tool.id == tool.id
    assert (
        tool.id,
        tool.name,
        tool.kind,
        Path(tool.root),
        tool.entry,
        tool.default_args,
        tool.default_env,
        tool.timeout,
    ) == (
        tool.id,
        "Echo helper",
        "python",
        entry.parent.resolve(),
        "echo.py",
        ("--format", "json"),
        {"TOOL_MODE": "record"},
        12.5,
    )

    argv, cwd = registry.resolve_command(tool.id, ["invoice 123"])
    assert argv == [
        sys.executable,
        str(entry.resolve()),
        "--format",
        "json",
        "invoice 123",
    ]
    assert cwd == entry.parent.resolve()

    dependencies = registry.tool_dependencies([tool.id])
    assert dependencies == {
        tool.id: {
            "id": tool.id,
            "name": "Echo helper",
            "kind": "python",
            "root": "helpers",
            "entry": "echo.py",
            "default_args": ["--format", "json"],
            "default_env": {"TOOL_MODE": "record"},
            "timeout": 12.5,
        }
    }
    json.dumps(dependencies)


@pytest.mark.parametrize(
    ("filename", "expected_kind", "expected_prefix"),
    [
        (
            "collect.ps1",
            "powershell",
            [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
            ],
        ),
        ("collect.js", "node", ["node"]),
        ("collect.exe", "exe", []),
    ],
)
def test_registry_resolves_supported_tools_to_argument_vectors(
    tmp_path: Path,
    filename: str,
    expected_kind: str,
    expected_prefix: list[str],
) -> None:
    workspace = tmp_path / "workspace"
    entry = workspace / "tools" / filename
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    registry = ExternalToolRegistry(workspace, workspace / ".studio" / "tools")

    tool = registry.register_path(entry)
    argv, cwd = registry.resolve_command(tool.id, ["--target", "QA tenant"])

    assert tool.kind == expected_kind
    assert argv == [
        *expected_prefix,
        str(entry.resolve()),
        "--target",
        "QA tenant",
    ]
    assert cwd == entry.parent.resolve()


def test_registry_requires_explicit_authorization_outside_workspace(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external_entry = _write_python(tmp_path / "shared" / "helper.py", "print('ok')\n")
    registry = ExternalToolRegistry(workspace, workspace / ".studio" / "tools")

    with pytest.raises(ValueError, match="explicit authorization"):
        registry.register_path(external_entry)

    tool = registry.register_path(external_entry, authorized=True)

    assert Path(tool.root) == external_entry.parent.resolve()


def test_registry_rejects_an_entry_that_escapes_its_declared_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    declared_root = workspace / "tools"
    declared_root.mkdir(parents=True)
    _write_python(workspace / "outside.py", "print('outside')\n")
    registry = ExternalToolRegistry(workspace, workspace / ".studio" / "tools")

    with pytest.raises(ValueError, match="escapes"):
        registry.register_path("../outside.py", root=declared_root)


def test_executor_runs_registered_tool_with_args_env_and_cwd(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    entry = _write_python(
        workspace / "tools" / "capture.py",
        "\n".join(
            [
                "import json",
                "import os",
                "import sys",
                "print(json.dumps({",
                "    'args': sys.argv[1:],",
                "    'default_env': os.environ.get('DEFAULT_ENV'),",
                "    'action_env': os.environ.get('ACTION_ENV'),",
                "    'cwd': os.getcwd(),",
                "}))",
            ]
        ),
    )
    run_dir = entry.parent / "run"
    run_dir.mkdir()
    registry = ExternalToolRegistry(workspace, workspace / ".studio" / "tools")
    tool = registry.register_path(
        entry,
        default_args=("--recorded",),
        default_env={"DEFAULT_ENV": "from manifest"},
        timeout=3,
    )
    executor = ExternalToolExecutor(registry)

    result = executor.execute(
        Action(
            name="run_external_tool",
            channel=ActionChannel.EXTERNAL,
            tool_ref=tool.id,
            arguments={
                "args": ["hello world", "&&", "literal"],
                "env": {"ACTION_ENV": "from action"},
                "timeout": 2,
                "cwd": "run",
            },
        )
    )

    assert result.status is ActionStatus.SUCCESS
    assert result.output["returncode"] == 0
    assert result.output["stderr"] == ""
    payload = json.loads(str(result.output["stdout"]))
    assert payload == {
        "args": ["--recorded", "hello world", "&&", "literal"],
        "default_env": "from manifest",
        "action_env": "from action",
        "cwd": str(run_dir.resolve()),
    }


def test_executor_maps_nonzero_exit_and_timeout_to_action_results(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    failed_entry = _write_python(
        workspace / "tools" / "fail.py",
        "import sys\nsys.stderr.write('broken\\n')\nraise SystemExit(7)\n",
    )
    slow_entry = _write_python(
        workspace / "tools" / "slow.py",
        "import time\ntime.sleep(2)\n",
    )
    registry = ExternalToolRegistry(workspace, workspace / ".studio" / "tools")
    failed_tool = registry.register_path(failed_entry)
    slow_tool = registry.register_path(slow_entry)
    executor = ExternalToolExecutor(registry)

    failed = executor.execute(
        Action(
            name="run_external_tool",
            channel=ActionChannel.EXTERNAL,
            tool_ref=failed_tool.id,
        )
    )
    timed_out = executor.execute(
        Action(
            name="run_external_tool",
            channel=ActionChannel.EXTERNAL,
            tool_ref=slow_tool.id,
            arguments={"timeout": 0.01},
        )
    )

    assert failed.status is ActionStatus.FAILED
    assert failed.output == {
        "stdout": "",
        "stderr": "broken\n",
        "returncode": 7,
    }
    assert "exit code 7" in failed.error
    assert timed_out.status is ActionStatus.TIMEOUT
    assert "timed out" in timed_out.error.lower()


def test_external_tool_process_is_terminated_on_cancellation(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    entry = _write_python(
        workspace / "tools" / "slow.py",
        "import time\ntime.sleep(30)\n",
    )
    registry = ExternalToolRegistry(workspace, workspace / ".studio" / "tools")
    tool = registry.register_path(entry)
    token = CancellationToken()
    context = ActionExecutionContext.create(token=token, timeout_ms=30_000)
    outcome = []
    worker = threading.Thread(
        target=lambda: outcome.append(
            ExternalToolExecutor(registry).execute(
                Action(
                    "run_external_tool",
                    ActionChannel.EXTERNAL,
                    tool_ref=tool.id,
                ),
                context,
            )
        ),
        daemon=True,
    )
    worker.start()

    assert token.cancel("Stopped by user")
    worker.join(2)

    assert worker.is_alive() is False
    assert outcome[0].status is ActionStatus.CANCELLED


def test_executor_terminates_running_process_when_action_is_cancelled(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    entry = _write_python(
        workspace / "tools" / "slow.py",
        "import time\ntime.sleep(30)\n",
    )
    registry = ExternalToolRegistry(workspace, workspace / ".studio" / "tools")
    tool = registry.register_path(entry, timeout=60)
    executor = ExternalToolExecutor(registry)
    cancellation = CancellationToken()
    control = ActionExecutionContext.create(cancellation, 60)
    result: list[ActionResult] = []
    worker = threading.Thread(
        target=lambda: result.append(
            executor.execute(
                Action(
                    "run_external_tool",
                    ActionChannel.EXTERNAL,
                    tool_ref=tool.id,
                ),
                control,
            )
        )
    )

    worker.start()
    time.sleep(0.05)
    cancellation.cancel("Cancelled by user")
    worker.join(2)

    assert not worker.is_alive()
    assert result[0].status is ActionStatus.CANCELLED


def test_executor_blocks_cwd_escape_before_starting_process(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    entry = _write_python(workspace / "tools" / "echo.py", "print('should not run')\n")
    registry = ExternalToolRegistry(workspace, workspace / ".studio" / "tools")
    tool = registry.register_path(entry)
    executor = ExternalToolExecutor(registry)

    result = executor.execute(
        Action(
            name="run_external_tool",
            channel=ActionChannel.EXTERNAL,
            tool_ref=tool.id,
            arguments={"cwd": ".."},
        )
    )

    assert result.status is ActionStatus.BLOCKED
    assert "escapes" in result.error


def test_executor_launches_registered_executable_without_waiting(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    workspace = tmp_path / "workspace"
    executable = workspace / "tools" / "desktop-app.exe"
    executable.parent.mkdir(parents=True)
    executable.touch()
    registry = ExternalToolRegistry(workspace, workspace / ".studio" / "tools")
    tool = registry.register_path(executable)
    captured = {}

    class _Process:
        pid = 4312

        def poll(self) -> None:
            return None

    def fake_popen(argv, **kwargs):  # noqa: ANN001, ANN003, ANN201
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return _Process()

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    result = ExternalToolExecutor(registry).execute(
        Action(
            "launch_application",
            ActionChannel.EXTERNAL,
            tool_ref=tool.id,
            arguments={"args": ["--profile", "recording"]},
        )
    )

    assert result.status is ActionStatus.SUCCESS
    assert result.output == {
        "pid": 4312,
        "tool_ref": tool.id,
        "running": True,
        "ownership": "detached",
    }
    assert captured["argv"] == [
        str(executable.resolve()),
        "--profile",
        "recording",
    ]
    assert captured["kwargs"]["shell"] is False


def test_executor_rejects_application_that_exits_during_startup(
    tmp_path: Path,
    monkeypatch,
) -> None:  # noqa: ANN001
    executable = tmp_path / "broken.exe"
    executable.write_bytes(b"MZ")
    registry = ExternalToolRegistry(tmp_path, tmp_path / ".tools")
    tool = registry.register_path(executable)

    class _ExitedProcess:
        pid = 55

        def poll(self) -> int:
            return 7

    monkeypatch.setattr(
        "subprocess.Popen", lambda *args, **kwargs: _ExitedProcess()
    )
    result = ExternalToolExecutor(registry).execute(
        Action(
            "launch_application",
            ActionChannel.EXTERNAL,
            tool_ref=tool.id,
        )
    )

    assert result.status is ActionStatus.FAILED
    assert "exit code 7" in result.error
