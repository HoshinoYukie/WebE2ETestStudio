"""Small configuration helpers shared by local application shells."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from tempfile import NamedTemporaryFile


def load_dotenv(path: str | Path, *, override: bool = False) -> set[str]:
    """Load simple KEY=VALUE entries without making dotenv a runtime dependency."""
    loaded: set[str] = set()
    for name, value in read_dotenv(path).items():
        if override or name not in os.environ:
            os.environ[name] = value
            loaded.add(name)
    return loaded


def read_dotenv(path: str | Path) -> dict[str, str]:
    """Read simple KEY=VALUE entries without changing the process environment."""
    source = Path(path)
    if not source.is_file():
        return {}
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        source.read_text(encoding="utf-8-sig").splitlines(), start=1
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
        values[name] = value
    return values


def configure_playwright_browsers(
    workspace: str | Path,
    *,
    application_root: str | Path | None = None,
    managed_roots: tuple[str | Path, ...] = (),
    bundled_roots: tuple[str | Path, ...] = (),
) -> Path | None:
    """Point Playwright at a managed local browser directory when available."""
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    candidates = [Path(root).expanduser().resolve() for root in managed_roots]
    candidates.append(Path(workspace).resolve() / ".playwright-browsers")
    candidates.extend(Path(root).expanduser().resolve() for root in bundled_roots)
    if application_root is not None:
        candidates.append(
            Path(application_root).resolve() / ".playwright-browsers"
        )
    for candidate in candidates:
        if candidate.is_dir() and any(candidate.glob("chromium-*")):
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(candidate)
            return candidate
    return None


def update_dotenv(
    path: str | Path,
    updates: Mapping[str, str | None],
) -> Path:
    """Atomically update selected dotenv values while preserving other entries."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    managed = set(updates)
    retained: list[str] = []
    if destination.is_file():
        for line in destination.read_text(encoding="utf-8-sig").splitlines():
            candidate = line.strip()
            if candidate.startswith("export "):
                candidate = candidate.removeprefix("export ").lstrip()
            name = candidate.split("=", 1)[0].strip() if "=" in candidate else ""
            if name not in managed:
                retained.append(line)
    while retained and not retained[-1].strip():
        retained.pop()
    if retained:
        retained.append("")
    retained.extend(
        f"{name}={json.dumps(value, ensure_ascii=False)}"
        for name, value in updates.items()
        if value is not None
    )
    content = "\n".join(retained) + "\n"
    with NamedTemporaryFile(
        "w",
        encoding="utf-8",
        newline="\n",
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(content)
        temporary = Path(handle.name)
    temporary.replace(destination)
    return destination


def _dotenv_value(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] == '"':
        return bytes(value[1:-1], "utf-8").decode("unicode_escape")
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1]
    comment = value.find(" #")
    return value[:comment].rstrip() if comment >= 0 else value


__all__ = [
    "configure_playwright_browsers",
    "load_dotenv",
    "read_dotenv",
    "update_dotenv",
]
