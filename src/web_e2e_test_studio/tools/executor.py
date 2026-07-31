"""Deterministic, shell-free execution of registered external tools."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Any

from web_e2e_test_studio.agent.control import ActionExecutionContext
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    ActionStatus,
    JsonValue,
)
from web_e2e_test_studio.tools.registry import ExternalToolRegistry


class ExternalToolExecutor:
    """Execute EXTERNAL actions through an ``ExternalToolRegistry``."""

    def __init__(self, registry: ExternalToolRegistry) -> None:
        self.registry = registry

    def execute(
        self,
        action: Action,
        control: ActionExecutionContext | None = None,
    ) -> ActionResult:
        if action.channel is not ActionChannel.EXTERNAL:
            return _blocked("ExternalToolExecutor only accepts external actions")
        if not action.tool_ref:
            return _blocked("External action tool_ref is required")

        try:
            args = _action_args(action)
            tool = self.registry.require(action.tool_ref)
            argv, _ = self.registry.resolve_command(tool.id, args)
            cwd = self.registry.resolve_cwd(tool.id, _action_cwd(action))
            timeout = _action_timeout(action, tool.timeout)
            env = os.environ.copy()
            env.update(tool.default_env)
            env.update(_action_env(action))
        except (TypeError, ValueError) as exc:
            return _blocked(str(exc))

        if action.name == "launch_application":
            if tool.kind != "exe":
                return _blocked(
                    "launch_application only accepts registered executables"
                )
            return self._launch_application(
                argv, cwd, env, tool.id, control
            )

        if control is not None:
            return self._execute_controlled(
                argv, cwd, env, timeout, tool.id, control
            )

        try:
            completed = subprocess.run(
                argv,
                cwd=cwd,
                env=env,
                timeout=timeout,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            output: dict[str, JsonValue] = {
                "stdout": _captured_text(exc.stdout),
                "stderr": _captured_text(exc.stderr),
                "returncode": None,
            }
            return ActionResult(
                status=ActionStatus.TIMEOUT,
                output=output,
                error=f"External tool {tool.id} timed out after {timeout:g} seconds",
            )
        except OSError as exc:
            return ActionResult(
                status=ActionStatus.FAILED,
                output={"stdout": "", "stderr": "", "returncode": None},
                error=f"External tool {tool.id} could not start: {exc}",
            )

        output = {
            "stdout": completed.stdout,
            "stderr": completed.stderr,
            "returncode": completed.returncode,
        }
        if completed.returncode != 0:
            return ActionResult(
                status=ActionStatus.FAILED,
                output=output,
                error=(
                    f"External tool {tool.id} exited with exit code "
                    f"{completed.returncode}"
                ),
            )
        return ActionResult.succeeded(output)

    @staticmethod
    def _launch_application(
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        tool_id: str,
        control: ActionExecutionContext | None,
    ) -> ActionResult:
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0
        )
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                shell=False,
                creationflags=creationflags,
            )
        except OSError as exc:
            return ActionResult.failed(
                f"Application {tool_id} could not start: {exc}"
            )
        stable_until = time.monotonic() + 0.25
        while time.monotonic() < stable_until:
            if control is not None and control.cancelled:
                _terminate_process_tree(process)
                return control.cancelled_result()
            returncode = process.poll()
            if returncode is not None:
                return ActionResult.failed(
                    f"Application {tool_id} exited during startup "
                    f"with exit code {returncode}"
                )
            time.sleep(0.025)
        return ActionResult.succeeded(
            {
                "tool_ref": tool_id,
                "pid": process.pid,
                "running": True,
                "ownership": "detached",
            }
        )

    @staticmethod
    def _execute_controlled(
        argv: list[str],
        cwd: Path,
        env: dict[str, str],
        timeout: float,
        tool_id: str,
        control: ActionExecutionContext,
    ) -> ActionResult:
        if control.cancelled:
            return control.cancelled_result()
        effective_timeout = control.remaining_seconds(timeout)
        if effective_timeout is None or effective_timeout <= 0:
            return control.timeout_result(
                f"External tool {tool_id} timed out before start"
            )
        started = time.monotonic()
        creationflags = (
            subprocess.CREATE_NEW_PROCESS_GROUP
            if os.name == "nt"
            else 0
        )
        try:
            process = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                shell=False,
                creationflags=creationflags,
            )
        except OSError as exc:
            return ActionResult.failed(
                f"External tool {tool_id} could not start: {exc}"
            )
        remove_cancel_callback = control.cancellation.add_cancel_callback(
            lambda: _terminate_process_tree(process)
        )
        try:
            while True:
                if control.cancellation.wait(0):
                    stdout, stderr = process.communicate()
                    return ActionResult(
                        ActionStatus.CANCELLED,
                        {
                            "stdout": stdout,
                            "stderr": stderr,
                            "returncode": process.returncode,
                        },
                        error=control.cancellation.reason,
                        error_category="cancelled",
                    )
                if (
                    control.timed_out
                    or time.monotonic() - started >= effective_timeout
                ):
                    _terminate_process_tree(process)
                    stdout, stderr = process.communicate()
                    return ActionResult(
                        ActionStatus.TIMEOUT,
                        {
                            "stdout": stdout,
                            "stderr": stderr,
                            "returncode": process.returncode,
                        },
                        error=(
                            f"External tool {tool_id} timed out after "
                            f"{effective_timeout:g} seconds"
                        ),
                        error_category="timeout",
                    )
                try:
                    stdout, stderr = process.communicate(timeout=0.05)
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            remove_cancel_callback()
        output: dict[str, JsonValue] = {
            "stdout": stdout,
            "stderr": stderr,
            "returncode": process.returncode,
        }
        if process.returncode != 0:
            return ActionResult(
                ActionStatus.FAILED,
                output,
                error=(
                    f"External tool {tool_id} exited with exit code "
                    f"{process.returncode}"
                ),
            )
        return ActionResult.succeeded(output)


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
            shell=False,
        )
    else:
        process.terminate()
    try:
        process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        process.kill()


def _action_args(action: Action) -> list[str]:
    raw = action.arguments.get("args", [])
    if not isinstance(raw, list):
        raise ValueError("External action args must be an array")
    args: list[str] = []
    for value in raw:
        if isinstance(value, (dict, list)) or value is None:
            raise ValueError("External action args must contain scalar values")
        args.append(str(value))
    return args


def _action_env(action: Action) -> dict[str, str]:
    raw = action.arguments.get("env", {})
    if not isinstance(raw, dict):
        raise ValueError("External action env must be an object")
    env: dict[str, str] = {}
    for raw_key, raw_value in raw.items():
        key = str(raw_key)
        if isinstance(raw_value, (dict, list)) or raw_value is None:
            raise ValueError("External action env values must be scalar")
        value = str(raw_value)
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError("External action env contains an invalid variable")
        env[key] = value
    return env


def _action_timeout(action: Action, default: float) -> float:
    raw = action.arguments.get("timeout", default)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError("External action timeout must be a number")
    timeout = float(raw)
    if timeout <= 0:
        raise ValueError("External action timeout must be greater than zero")
    return timeout


def _action_cwd(action: Action) -> str | Path | None:
    raw = action.arguments.get("cwd")
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("External action cwd must be a string")
    return raw


def _captured_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _blocked(error: str) -> ActionResult:
    return ActionResult(status=ActionStatus.BLOCKED, error=error)


__all__ = ["ExternalToolExecutor"]
