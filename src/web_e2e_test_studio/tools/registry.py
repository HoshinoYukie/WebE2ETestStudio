"""Workspace-scoped registration and argv resolution for external tools."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from web_e2e_test_studio.domain import JsonValue
from web_e2e_test_studio.tools.models import SUPPORTED_TOOL_KINDS, ExternalTool

_KIND_BY_SUFFIX = {
    ".py": "python",
    ".pyw": "python",
    ".ps1": "powershell",
    ".js": "node",
    ".mjs": "node",
    ".cjs": "node",
    ".exe": "exe",
}
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


class ExternalToolRegistry:
    """Registry whose default trust boundary is one workspace directory."""

    def __init__(
        self,
        workspace_root: str | Path,
        manifest_dir: str | Path,
    ) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        raw_manifest_dir = Path(manifest_dir)
        self.manifest_dir = (
            raw_manifest_dir
            if raw_manifest_dir.is_absolute()
            else self.workspace_root / raw_manifest_dir
        ).resolve()
        self.manifest_dir.mkdir(parents=True, exist_ok=True)
        self._tools: dict[str, ExternalTool] = {}
        self._load_workspace_manifests()

    def register_path(
        self,
        path: str | Path,
        *,
        name: str = "",
        kind: str = "",
        root: str | Path | None = None,
        default_args: Sequence[str] = (),
        default_env: Mapping[str, str] | None = None,
        timeout: float = 30.0,
        authorized: bool = False,
        tool_id: str | None = None,
        id: str | None = None,
    ) -> ExternalTool:
        """Register a file, requiring opt-in for paths outside the workspace."""
        if tool_id is not None and id is not None and tool_id != id:
            raise ValueError("Conflicting external tool ids")
        requested_id = tool_id or id
        declared_root = self._resolve_declared_root(root, path)
        entry_path = self._resolve_entry(declared_root, path, root is not None)
        self._require_authorized(declared_root, authorized)
        self._require_authorized(entry_path, authorized)
        if not entry_path.is_file():
            raise ValueError(f"External tool entry not found: {path}")

        resolved_kind = (kind or _KIND_BY_SUFFIX.get(entry_path.suffix.lower(), "")).lower()
        if resolved_kind not in SUPPORTED_TOOL_KINDS:
            raise ValueError(
                f"Unsupported external tool kind for entry: {entry_path.name}"
            )
        args = _string_tuple(default_args, "default_args")
        env = _string_mapping(default_env or {}, "default_env")
        if isinstance(timeout, bool) or float(timeout) <= 0:
            raise ValueError("External tool timeout must be greater than zero")

        for existing in self._tools.values():
            if self._entry_path(existing) == entry_path:
                return existing

        stable_id = requested_id or self._stable_id(entry_path)
        _validate_tool_id(stable_id)
        tool = ExternalTool(
            id=stable_id,
            name=name.strip() or entry_path.stem,
            kind=resolved_kind,
            root=str(declared_root),
            entry=entry_path.relative_to(declared_root).as_posix(),
            default_args=args,
            default_env=env,
            timeout=float(timeout),
        )
        current = self._tools.get(tool.id)
        if current is not None and current != tool:
            raise ValueError(f"External tool id is already registered: {tool.id}")
        self._tools[tool.id] = tool
        self._write_manifest(tool)
        return tool

    def register(
        self,
        path: str | Path,
        **kwargs: Any,
    ) -> ExternalTool:
        """Alias for :meth:`register_path` used by composition layers."""
        return self.register_path(path, **kwargs)

    def get(self, tool_ref: str) -> ExternalTool | None:
        return self._tools.get(tool_ref)

    def get_tool(self, tool_ref: str) -> ExternalTool | None:
        """Compatibility alias for callers that use resource terminology."""
        return self.get(tool_ref)

    def require(self, tool_ref: str) -> ExternalTool:
        tool = self.get(tool_ref)
        if tool is None:
            raise ValueError(f"External tool not found: {tool_ref}")
        return tool

    def list_tools(self) -> list[ExternalTool]:
        return [self._tools[key] for key in sorted(self._tools)]

    def resolve_command(
        self,
        tool_ref: str,
        arguments: Sequence[str] | None = None,
    ) -> tuple[list[str], Path]:
        """Return a shell-free argv plus the tool's bounded working root."""
        tool = self.require(tool_ref)
        entry = self._entry_path(tool)
        args = [*tool.default_args, *_string_tuple(arguments or (), "args")]
        if tool.kind == "python":
            argv = [sys.executable, str(entry), *args]
        elif tool.kind == "powershell":
            argv = [
                "powershell.exe",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(entry),
                *args,
            ]
        elif tool.kind == "node":
            argv = ["node", str(entry), *args]
        elif tool.kind == "exe":
            argv = [str(entry), *args]
        else:  # ExternalTool validates kinds; keep this boundary defensive.
            raise ValueError(f"Unsupported external tool kind: {tool.kind}")
        return argv, Path(tool.root).resolve()

    def resolve_cwd(
        self,
        tool_ref: str,
        cwd: str | Path | None = None,
    ) -> Path:
        """Resolve an action cwd without allowing it to leave the tool root."""
        tool = self.require(tool_ref)
        root = Path(tool.root).resolve()
        candidate = (
            root
            if cwd is None or str(cwd).strip() in {"", "."}
            else (root / cwd).resolve()
        )
        _require_under(candidate, root, "External tool cwd escapes its tool root")
        if not candidate.is_dir():
            raise ValueError(f"External tool cwd not found: {cwd}")
        return candidate

    def portable_manifest(self, tool_ref: str) -> dict[str, JsonValue]:
        """Return one JSON-compatible dependency relative to the workspace."""
        tool = self.require(tool_ref)
        root = Path(tool.root).resolve()
        if _is_relative_to(root, self.workspace_root):
            portable_root = root.relative_to(self.workspace_root).as_posix() or "."
        else:
            # An explicitly authorized reference remains executable locally.
            # Exporters can use this source location to bundle it later.
            portable_root = root.as_posix()
        return tool.to_dict(root=portable_root)

    def manifest(self, tool_ref: str) -> dict[str, JsonValue]:
        """Short alias for callers embedding one dependency."""
        return self.portable_manifest(tool_ref)

    def tool_dependencies(
        self,
        tool_refs: Iterable[str] | None = None,
    ) -> dict[str, dict[str, JsonValue]]:
        """Build the mapping accepted by ``TestAsset.tool_dependencies``."""
        refs = (
            sorted(self._tools)
            if tool_refs is None
            else sorted(set(tool_refs))
        )
        return {tool_ref: self.portable_manifest(tool_ref) for tool_ref in refs}

    def _resolve_declared_root(
        self,
        root: str | Path | None,
        path: str | Path,
    ) -> Path:
        if root is not None:
            raw_root = Path(root)
            return (
                raw_root
                if raw_root.is_absolute()
                else self.workspace_root / raw_root
            ).resolve()
        raw_path = Path(path)
        entry = (
            raw_path
            if raw_path.is_absolute()
            else self.workspace_root / raw_path
        ).resolve()
        return entry.parent

    def _resolve_entry(
        self,
        root: Path,
        path: str | Path,
        root_was_explicit: bool,
    ) -> Path:
        raw_path = Path(path)
        if root_was_explicit and not raw_path.is_absolute():
            candidate = (root / raw_path).resolve()
        elif raw_path.is_absolute():
            candidate = raw_path.resolve()
        else:
            candidate = (self.workspace_root / raw_path).resolve()
        _require_under(candidate, root, "External tool entry escapes its tool root")
        return candidate

    def _require_authorized(self, path: Path, authorized: bool) -> None:
        if authorized or _is_relative_to(path, self.workspace_root):
            return
        raise ValueError(
            "Path is outside the workspace and requires explicit authorization"
        )

    def _entry_path(self, tool: ExternalTool) -> Path:
        root = Path(tool.root).resolve()
        entry = (root / tool.entry).resolve()
        _require_under(entry, root, "External tool entry escapes its tool root")
        if not entry.is_file():
            raise ValueError(f"External tool entry not found: {tool.entry}")
        return entry

    def _stable_id(self, entry: Path) -> str:
        if _is_relative_to(entry, self.workspace_root):
            identity = entry.relative_to(self.workspace_root).as_posix()
        else:
            identity = entry.as_posix()
        digest = hashlib.sha256(identity.casefold().encode("utf-8")).hexdigest()[:12]
        return f"tool_{digest}"

    def _manifest_path(self, tool_id: str) -> Path:
        _validate_tool_id(tool_id)
        path = (self.manifest_dir / f"{tool_id}.json").resolve()
        _require_under(path, self.manifest_dir, "External tool manifest path escapes")
        return path

    def _write_manifest(self, tool: ExternalTool) -> None:
        self._manifest_path(tool.id).write_text(
            json.dumps(
                self.portable_manifest(tool.id),
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    def _load_workspace_manifests(self) -> None:
        for path in sorted(self.manifest_dir.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    continue
                manifest_root = Path(str(raw["root"]))
                if manifest_root.is_absolute():
                    # Persisted outside-workspace references require a fresh
                    # explicit authorization and are deliberately not reloaded.
                    continue
                resolved_root = (self.workspace_root / manifest_root).resolve()
                if not _is_relative_to(resolved_root, self.workspace_root):
                    continue
                tool = ExternalTool.from_dict(raw, root=resolved_root)
                _validate_tool_id(tool.id)
                self._entry_path(tool)
                self._tools[tool.id] = tool
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                continue


def _string_tuple(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not all(
        isinstance(value, str) for value in values
    ):
        raise ValueError(f"External tool {label} must be a string array")
    return tuple(values)


def _string_mapping(values: Mapping[str, str], label: str) -> dict[str, str]:
    if not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in values.items()
    ):
        raise ValueError(f"External tool {label} must be a string object")
    for key, value in values.items():
        if not key or "=" in key or "\0" in key or "\0" in value:
            raise ValueError(f"External tool {label} contains an invalid variable")
    return dict(values)


def _validate_tool_id(tool_id: str) -> None:
    if not _SAFE_ID.fullmatch(tool_id):
        raise ValueError(f"Unsafe external tool id: {tool_id}")


def _require_under(path: Path, root: Path, error: str) -> None:
    if not _is_relative_to(path, root):
        raise ValueError(error)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


__all__ = ["ExternalToolRegistry"]
