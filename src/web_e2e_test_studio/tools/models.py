"""Serializable descriptions of deterministic external tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from web_e2e_test_studio.domain import JsonValue

SUPPORTED_TOOL_KINDS = frozenset({"python", "powershell", "node", "exe"})


@dataclass(frozen=True)
class ExternalTool:
    """A stable reference to one executable entry inside a bounded root."""

    id: str
    name: str
    kind: str
    root: str
    entry: str
    default_args: tuple[str, ...] = ()
    default_env: dict[str, str] = field(default_factory=dict)
    timeout: float = 30.0

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("External tool id is required")
        if not self.name:
            raise ValueError("External tool name is required")
        if self.kind not in SUPPORTED_TOOL_KINDS:
            raise ValueError(f"Unsupported external tool kind: {self.kind}")
        if not self.root:
            raise ValueError("External tool root is required")
        entry = Path(self.entry)
        if not self.entry or entry.is_absolute() or ".." in entry.parts:
            raise ValueError("External tool entry must be a safe relative path")
        if self.timeout <= 0:
            raise ValueError("External tool timeout must be greater than zero")

    def to_dict(self, *, root: str | None = None) -> dict[str, JsonValue]:
        """Return a JSON-compatible manifest description."""
        return {
            "id": self.id,
            "name": self.name,
            "kind": self.kind,
            "root": root if root is not None else self.root,
            "entry": self.entry,
            "default_args": list(self.default_args),
            "default_env": dict(self.default_env),
            "timeout": self.timeout,
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        *,
        root: str | Path | None = None,
    ) -> ExternalTool:
        """Build a tool from a persisted or embedded manifest."""
        raw_args = data.get("default_args", [])
        if not isinstance(raw_args, list) or not all(
            isinstance(item, str) for item in raw_args
        ):
            raise ValueError("External tool default_args must be a string array")
        raw_env = data.get("default_env", {})
        if not isinstance(raw_env, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in raw_env.items()
        ):
            raise ValueError("External tool default_env must be a string object")
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            kind=str(data["kind"]),
            root=str(root if root is not None else data["root"]),
            entry=str(data["entry"]),
            default_args=tuple(raw_args),
            default_env=dict(raw_env),
            timeout=float(data.get("timeout", 30.0)),
        )


__all__ = ["ExternalTool", "SUPPORTED_TOOL_KINDS"]
