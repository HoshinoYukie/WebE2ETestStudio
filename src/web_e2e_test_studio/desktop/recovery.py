"""Atomic, secret-safe checkpoints for recoverable Desktop authoring runs."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import uuid4

from web_e2e_test_studio.agent.lifecycle import (
    TaskFailure,
    TaskStateSnapshot,
)
from web_e2e_test_studio.domain import JsonValue, utc_now

_STATES = {
    "active",
    "running",
    "waiting_for_user",
    "suspended",
    "failed",
    "cancelled",
}
_SECRET_KEYS = {
    "access_token",
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "client_secret",
    "cookie",
    "password",
    "refresh_token",
    "secret",
    "token",
    "x-api-key",
}
_DISCOVERY_EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".playwright-browsers",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}


@dataclass(frozen=True)
class RunCheckpoint:
    """Everything needed to reopen a persisted authoring session."""

    id: str
    name: str
    goal: str
    asset_path: Path
    output_path: Path
    workspace: Path
    initial_url: str = ""
    headless: bool = False
    state: str = "active"
    active_instruction: str = ""
    pending_question: str = ""
    round_number: int = 0
    last_url: str = ""
    last_error: str = ""
    memories: tuple[str, ...] = ()
    task_lifecycles: dict[str, TaskStateSnapshot] = field(
        default_factory=dict
    )
    last_failure: TaskFailure | None = None
    events: tuple[dict[str, JsonValue], ...] = ()
    updated_at: str = field(default_factory=utc_now)
    schema: str = "web_e2e_run_checkpoint.v1"

    def __post_init__(self) -> None:
        if self.state not in _STATES:
            raise ValueError(f"Unsupported checkpoint state: {self.state}")
        if self.round_number < 0:
            raise ValueError("Checkpoint round cannot be negative")

    @classmethod
    def create(
        cls,
        *,
        name: str,
        goal: str,
        asset_path: Path,
        output_path: Path,
        workspace: Path,
        initial_url: str,
        headless: bool,
    ) -> RunCheckpoint:
        return cls(
            id=f"run_{uuid4().hex[:12]}",
            name=name,
            goal=goal,
            asset_path=asset_path.resolve(),
            output_path=output_path.resolve(),
            workspace=workspace.resolve(),
            initial_url=initial_url,
            headless=headless,
            last_url=initial_url,
        )

    def updated(self, **changes: Any) -> RunCheckpoint:  # noqa: ANN401
        return replace(self, updated_at=utc_now(), **changes)

    def with_event(
        self,
        kind: str,
        payload: dict[str, JsonValue],
    ) -> RunCheckpoint:
        event: dict[str, JsonValue] = {
            "kind": kind,
            "payload": _sanitize(payload),
            "at": utc_now(),
        }
        return self.updated(events=(*self.events[-199:], event))

    @property
    def checkpoint_path(self) -> Path:
        try:
            self.asset_path.relative_to(self.workspace)
        except ValueError:
            return self.workspace / ".web-e2e" / "runs" / f"{self.id}.run.json"
        return self.asset_path.with_suffix(".run.json")

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema": self.schema,
            "id": self.id,
            "name": self.name,
            "goal": self.goal,
            "asset_path": str(self.asset_path),
            "output_path": str(self.output_path),
            "workspace": str(self.workspace),
            "initial_url": self.initial_url,
            "headless": self.headless,
            "state": self.state,
            "active_instruction": self.active_instruction,
            "pending_question": self.pending_question,
            "round_number": self.round_number,
            "last_url": self.last_url,
            "last_error": self.last_error,
            "memories": list(self.memories),
            "task_lifecycles": {
                task_id: state.to_dict()
                for task_id, state in self.task_lifecycles.items()
            },
            "last_failure": (
                self.last_failure.to_dict() if self.last_failure else None
            ),
            "events": [_sanitize(event) for event in self.events],
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunCheckpoint:
        if data.get("schema") != "web_e2e_run_checkpoint.v1":
            raise ValueError("Unsupported run checkpoint schema")
        raw_events = data.get("events", [])
        return cls(
            id=str(data["id"]),
            name=str(data["name"]),
            goal=str(data.get("goal", "")),
            asset_path=Path(str(data["asset_path"])).resolve(),
            output_path=Path(str(data["output_path"])).resolve(),
            workspace=Path(str(data["workspace"])).resolve(),
            initial_url=str(data.get("initial_url", "")),
            headless=bool(data.get("headless", False)),
            state=str(data.get("state", "suspended")),
            active_instruction=str(data.get("active_instruction", "")),
            pending_question=str(data.get("pending_question", "")),
            round_number=int(data.get("round_number", 0)),
            last_url=str(data.get("last_url", "")),
            last_error=str(data.get("last_error", "")),
            memories=tuple(
                str(item)[:4_000]
                for item in data.get("memories", [])
                if isinstance(item, str) and item.strip()
            )
            if isinstance(data.get("memories", []), list)
            else (),
            task_lifecycles={
                str(task_id): TaskStateSnapshot.from_dict(value)
                for task_id, value in data.get(
                    "task_lifecycles", {}
                ).items()
                if isinstance(value, dict)
            }
            if isinstance(data.get("task_lifecycles", {}), dict)
            else {},
            last_failure=(
                TaskFailure.from_dict(data["last_failure"])
                if isinstance(data.get("last_failure"), dict)
                else None
            ),
            events=tuple(
                dict(item)
                for item in raw_events
                if isinstance(item, dict)
            )
            if isinstance(raw_events, list)
            else (),
            updated_at=str(data.get("updated_at", "")) or utc_now(),
        )


class RunCheckpointStore:
    """Persist and discover checkpoints behind one workspace-scoped interface."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    def save(self, checkpoint: RunCheckpoint) -> Path:
        target = checkpoint.checkpoint_path
        self._require_workspace_path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(
            checkpoint.to_dict(),
            ensure_ascii=False,
            indent=2,
        )
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="\n",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(content)
            handle.write("\n")
            temporary = Path(handle.name)
        temporary.replace(target)
        return target

    def load(self, path: str | Path) -> RunCheckpoint:
        target = Path(path).resolve()
        self._require_workspace_path(target)
        raw = json.loads(target.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError("Run checkpoint must contain a JSON object")
        checkpoint = RunCheckpoint.from_dict(raw)
        if checkpoint.workspace != self.workspace:
            raise ValueError("Run checkpoint belongs to a different workspace")
        if checkpoint.checkpoint_path != target:
            raise ValueError("Run checkpoint path does not match its identity")
        return checkpoint

    def discover(self) -> tuple[RunCheckpoint, ...]:
        checkpoints: list[RunCheckpoint] = []
        for root, directories, files in os.walk(self.workspace):
            directories[:] = [
                name
                for name in directories
                if name not in _DISCOVERY_EXCLUDED_DIRS
            ]
            for filename in files:
                if not filename.endswith(".run.json"):
                    continue
                try:
                    checkpoint = self.load(Path(root) / filename)
                except (
                    OSError,
                    ValueError,
                    KeyError,
                    TypeError,
                    json.JSONDecodeError,
                ):
                    continue
                if checkpoint.asset_path.is_file():
                    checkpoints.append(checkpoint)
        return tuple(
            sorted(
                checkpoints,
                key=lambda item: item.updated_at,
                reverse=True,
            )
        )

    def discard(self, path: str | Path) -> None:
        target = Path(path).resolve()
        self._require_workspace_path(target)
        if target.suffixes[-2:] != [".run", ".json"]:
            raise ValueError("Only .run.json checkpoints can be discarded")
        target.unlink(missing_ok=True)

    def _require_workspace_path(self, path: Path) -> None:
        try:
            path.resolve().relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError(
                f"Checkpoint path must stay inside workspace: {path}"
            ) from exc


def _sanitize(value: JsonValue, *, key: str = "") -> JsonValue:
    if key.casefold() in _SECRET_KEYS:
        return "[REDACTED]"
    if isinstance(value, str):
        return value[:4_000]
    if isinstance(value, list):
        return [_sanitize(item) for item in value[:200]]
    if isinstance(value, dict):
        return {
            item_key: _sanitize(item_value, key=item_key)
            for item_key, item_value in list(value.items())[:200]
        }
    return value


__all__ = ["RunCheckpoint", "RunCheckpointStore"]
