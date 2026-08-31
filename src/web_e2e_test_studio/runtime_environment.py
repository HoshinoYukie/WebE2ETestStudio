"""Product-generic runtime diagnostics and repair orchestration."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from web_e2e_test_studio.runtime_paths import RuntimePaths


class RuntimeEnvironmentManager:
    """Inspect the runtime required by the packaged Desktop application."""

    def __init__(
        self,
        workspace: str | Path,
        paths: RuntimePaths,
        *,
        environment: Mapping[str, str] | None = None,
        runtime: Any = sys,  # noqa: ANN401
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.paths = paths
        self.environment = os.environ if environment is None else environment
        self.runtime = runtime
        self.command_runner = command_runner

    @property
    def browser_root(self) -> Path:
        configured = self.environment.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
        return (
            Path(configured).expanduser().resolve()
            if configured
            else self.paths.browser_dir.resolve()
        )

    def inspect(self) -> dict[str, Any]:
        checks = [
            _writable_check(
                "application_data",
                "Application data",
                self.paths.data_dir,
            ),
            _writable_check("workspace", "Project workspace", self.workspace),
            _playwright_check(),
            _chromium_check(self.browser_root),
            _resource_check(self.paths.web_entrypoint),
        ]
        return {
            "ready": all(check["status"] == "ready" for check in checks),
            "checks": checks,
        }

    def install_chromium(self) -> dict[str, Any]:
        """Install Chromium into the per-user managed browser directory."""
        browser_root = self.paths.browser_dir.resolve()
        browser_root.mkdir(parents=True, exist_ok=True)
        executable = str(getattr(self.runtime, "executable", sys.executable))
        if bool(getattr(self.runtime, "frozen", False)):
            command = [
                executable,
                "--environment-tool",
                "install-chromium",
                "--browser-path",
                str(browser_root),
            ]
        else:
            command = [
                executable,
                "-m",
                "web_e2e_test_studio.environment_cli",
                "install-chromium",
                "--browser-path",
                str(browser_root),
            ]
        child_environment = dict(self.environment)
        child_environment["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
        completed = self.command_runner(
            command,
            env=child_environment,
            capture_output=True,
            text=True,
            timeout=1200,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "installation failed").strip()
            raise RuntimeError(f"Chromium installation failed: {detail[-1000:]}")
        if isinstance(self.environment, dict):
            self.environment["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_root)
        return {
            "installed": True,
            "message": "Playwright Chromium installed",
            "environment": self.inspect(),
        }


def _writable_check(check_id: str, label: str, path: Path) -> dict[str, Any]:
    try:
        path.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path, prefix=".environment-check-", delete=False) as handle:
            probe = Path(handle.name)
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return _check(
            check_id,
            label,
            "error",
            f"Directory is not writable: {exc}",
        )
    return _check(check_id, label, "ready", f"Writable: {path}")


def _playwright_check() -> dict[str, Any]:
    available = importlib.util.find_spec("playwright") is not None
    return _check(
        "playwright_runtime",
        "Playwright runtime",
        "ready" if available else "missing",
        "Playwright Python runtime is available"
        if available
        else "Playwright Python runtime is missing from this installation",
    )


def _chromium_check(browser_root: Path) -> dict[str, Any]:
    installed = browser_root.is_dir() and any(browser_root.glob("chromium-*"))
    if installed:
        return _check(
            "chromium",
            "Playwright Chromium",
            "ready",
            f"Installed in {browser_root}",
        )
    return _check(
        "chromium",
        "Playwright Chromium",
        "missing",
        "Chromium is not installed in the managed browser directory",
        repairable=True,
    )


def _resource_check(entrypoint: Path) -> dict[str, Any]:
    available = entrypoint.is_file()
    return _check(
        "desktop_resources",
        "Desktop resources",
        "ready" if available else "error",
        f"Desktop resources found at {entrypoint}"
        if available
        else f"Desktop entrypoint is missing: {entrypoint}",
    )


def _check(
    check_id: str,
    label: str,
    status: str,
    message: str,
    *,
    repairable: bool = False,
) -> dict[str, Any]:
    return {
        "id": check_id,
        "label": label,
        "status": status,
        "message": message,
        "repairable": repairable,
    }


__all__: Sequence[str] = ["RuntimeEnvironmentManager"]
