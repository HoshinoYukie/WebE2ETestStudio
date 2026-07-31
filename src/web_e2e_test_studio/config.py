"""Small configuration helpers shared by local application shells."""

from __future__ import annotations

import os
from pathlib import Path


def load_dotenv(path: str | Path, *, override: bool = False) -> set[str]:
    """Load simple KEY=VALUE entries without making dotenv a runtime dependency."""
    source = Path(path)
    if not source.is_file():
        return set()
    loaded: set[str] = set()
    for line_number, raw_line in enumerate(
        source.read_text(encoding="utf-8-sig").splitlines(),
        start=1,
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        if "=" not in line:
            raise ValueError(
                f"Invalid .env entry at {source}:{line_number}"
            )
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not name or not name.replace("_", "a").isalnum():
            raise ValueError(
                f"Invalid .env variable at {source}:{line_number}"
            )
        value = _dotenv_value(raw_value.strip())
        if override or name not in os.environ:
            os.environ[name] = value
            loaded.add(name)
    return loaded


def configure_playwright_browsers(
    workspace: str | Path,
    *,
    application_root: str | Path | None = None,
) -> Path | None:
    """Point Playwright at a managed local browser directory when available."""
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    candidates = [Path(workspace).resolve() / ".playwright-browsers"]
    if application_root is not None:
        candidates.append(
            Path(application_root).resolve() / ".playwright-browsers"
        )
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("chromium-*")):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(candidate)
            return candidate
    return None


def _dotenv_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return bytes(value[1:-1], "utf-8").decode("unicode_escape")
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    comment = value.find(" #")
    return value[:comment].rstrip() if comment >= 0 else value


__all__ = ["configure_playwright_browsers", "load_dotenv"]
