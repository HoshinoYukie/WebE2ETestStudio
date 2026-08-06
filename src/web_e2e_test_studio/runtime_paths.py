"""Runtime and writable path discovery for source and packaged builds."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_APPLICATION_NAME = "WebE2ETestStudio"


@dataclass(frozen=True)
class RuntimePaths:
    """Resolved application resources and per-user writable directories."""

    application_dir: Path
    bundle_dir: Path
    web_dir: Path
    data_dir: Path

    @classmethod
    def discover(
        cls,
        *,
        environment: Mapping[str, str] | None = None,
        platform: str | None = None,
        runtime: Any = sys,  # noqa: ANN401
    ) -> RuntimePaths:
        env = os.environ if environment is None else environment
        current_platform = sys.platform if platform is None else platform
        frozen = bool(getattr(runtime, "frozen", False))
        bundle_value = getattr(runtime, "_MEIPASS", "")
        bundle_dir = (
            Path(str(bundle_value)).resolve()
            if bundle_value
            else Path(__file__).resolve().parents[2]
        )
        application_dir = (
            Path(str(getattr(runtime, "executable", sys.executable))).resolve().parent
            if frozen
            else Path(__file__).resolve().parents[2]
        )
        packaged_web = bundle_dir / "web_e2e_test_studio" / "desktop" / "web"
        source_web = Path(__file__).resolve().parent / "desktop" / "web"
        web_dir = packaged_web if packaged_web.is_dir() else source_web
        data_dir = _data_directory(env, current_platform)
        return cls(
            application_dir=application_dir,
            bundle_dir=bundle_dir,
            web_dir=web_dir.resolve(),
            data_dir=data_dir.resolve(),
        )

    @property
    def web_entrypoint(self) -> Path:
        return self.web_dir / "index.html"

    @property
    def config_dir(self) -> Path:
        return self.data_dir / "config"

    @property
    def user_env_path(self) -> Path:
        return self.config_dir / ".env"

    @property
    def browser_dir(self) -> Path:
        return self.data_dir / "browsers"

    @property
    def session_dir(self) -> Path:
        return self.data_dir / "sessions"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def bundled_browser_candidates(self) -> tuple[Path, ...]:
        return (
            self.application_dir / "runtime" / "playwright",
            self.bundle_dir / "runtime" / "playwright",
            self.application_dir / ".playwright-browsers",
        )

    def ensure(self) -> None:
        """Create every writable application directory."""
        for path in (
            self.config_dir,
            self.browser_dir,
            self.session_dir,
            self.log_dir,
            self.cache_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _data_directory(environment: Mapping[str, str], platform: str) -> Path:
    override = environment.get("WEB_E2E_STUDIO_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if platform == "win32":
        local_app_data = environment.get("LOCALAPPDATA", "").strip()
        if local_app_data:
            return Path(local_app_data) / _APPLICATION_NAME
    xdg_data = environment.get("XDG_DATA_HOME", "").strip()
    if xdg_data:
        return Path(xdg_data) / _APPLICATION_NAME
    return Path.home() / ".local" / "share" / _APPLICATION_NAME


__all__ = ["RuntimePaths"]
