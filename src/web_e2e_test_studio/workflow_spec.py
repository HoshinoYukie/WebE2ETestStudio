"""Markdown-backed, business-editable workflow specifications."""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from web_e2e_test_studio.domain import JsonValue

_WORKFLOW_BLOCK = re.compile(
    r"(?P<open>```yaml[ \t]+workflow[ \t]*\r?\n)"
    r"(?P<body>.*?)"
    r"(?P<close>\r?\n```)",
    re.DOTALL | re.IGNORECASE,
)
_REFERENCE = re.compile(
    r"\$\{(?P<kind>data|ENV):(?P<colon>[A-Za-z_][A-Za-z0-9_.-]*)\}"
    r"|\$\{data\.(?P<dot>[A-Za-z_][A-Za-z0-9_.-]*)\}"
)
_CONDITION = re.compile(
    r"^data\.(?P<field>[A-Za-z_][A-Za-z0-9_.-]*)\s*"
    r"(?P<operator>==|!=|in|not in)\s*(?P<expected>.+)$"
)


@dataclass(frozen=True)
class CompiledWorkflow:
    """Resolved input for one Playwright authoring run."""

    name: str
    start_url: str
    goal: str
    steps: tuple[str, ...]
    memories: tuple[str, ...]
    reference_notes: str
    secret_values: dict[str, str]
    data: dict[str, JsonValue]
    parameterized_start_url: str
    parameterized_goal: str
    parameterized_steps: tuple[str, ...]

    @property
    def instruction(self) -> str:
        numbered = "\n".join(
            f"{index}. {step}" for index, step in enumerate(self.steps, start=1)
        )
        return (
            f"Business goal: {self.goal}\n\n"
            "Execute these business stages in order using the registered "
            "Playwright browser actions. Observe the live page before each "
            "decision and use its accessible elements; do not reuse Selenium "
            f"selectors or execute legacy test code.\n\n{numbered}"
        )

    def sanitized_instruction(self) -> str:
        value = self.instruction
        for secret, reference in sorted(
            self.secret_values.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if secret:
                value = value.replace(secret, reference)
        return value

    @property
    def authoring_instruction(self) -> str:
        """Instruction whose data values remain portable runtime references."""
        numbered = "\n".join(
            f"{index}. {step}"
            for index, step in enumerate(self.parameterized_steps, start=1)
        )
        return (
            f"Business goal: {self.parameterized_goal}\n\n"
            "Execute these business stages in order using the registered "
            "Playwright browser actions. Preserve every ${INPUT:path} token "
            "verbatim in action arguments; the authoring runtime resolves it "
            "to the current sample value before execution. Observe the live "
            "page before each decision and use its accessible elements; do "
            "not reuse Selenium selectors or execute legacy test code.\n\n"
            f"{numbered}"
        )


@dataclass(frozen=True)
class MarkdownWorkflowSpec:
    """A Markdown document containing one ``yaml workflow`` block."""

    text: str
    config: dict[str, Any]
    block_start: int
    block_end: int
    block_open: str
    block_close: str

    @classmethod
    def from_text(cls, text: str) -> MarkdownWorkflowSpec:
        matches = list(_WORKFLOW_BLOCK.finditer(text))
        if len(matches) != 1:
            raise ValueError(
                "Workflow Markdown must contain exactly one ```yaml workflow block"
            )
        match = matches[0]
        loaded = yaml.safe_load(match.group("body"))
        if not isinstance(loaded, dict):
            raise ValueError("Workflow YAML must contain an object")
        config = {str(key): value for key, value in loaded.items()}
        if config.get("schema") != "web_e2e_workflow.v1":
            raise ValueError("Unsupported workflow schema")
        for key, label in (
            ("name", "Workflow name"),
            ("start_url", "Start URL"),
            ("goal", "Workflow goal"),
        ):
            _require_text(config.get(key), label)
        data = config.get("data", {})
        if not isinstance(data, dict):
            raise ValueError("Workflow data must be an object")
        if "steps" not in config:
            raise ValueError("Workflow steps are required")
        for collection in ("steps", "rules", "validations"):
            value = config.get(collection, [])
            if not isinstance(value, list):
                raise ValueError(f"Workflow {collection} must be a list")
        return cls(
            text=text,
            config=config,
            block_start=match.start(),
            block_end=match.end(),
            block_open=match.group("open"),
            block_close=match.group("close"),
        )

    @classmethod
    def load(cls, path: str | Path) -> MarkdownWorkflowSpec:
        return cls.from_text(Path(path).read_text(encoding="utf-8-sig"))

    @property
    def data(self) -> dict[str, JsonValue]:
        return cast(dict[str, JsonValue], dict(self.config.get("data", {})))

    def with_data(self, updates: Mapping[str, JsonValue]) -> MarkdownWorkflowSpec:
        config = dict(self.config)
        config["data"] = {**self.data, **updates}
        body = yaml.safe_dump(
            config,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        ).rstrip()
        block = f"{self.block_open}{body}{self.block_close}"
        return self.from_text(
            self.text[: self.block_start] + block + self.text[self.block_end :]
        )

    def save(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(self.text, encoding="utf-8", newline="\n")
        return destination

    def compile(
        self,
        *,
        environment: Mapping[str, str] | None = None,
    ) -> CompiledWorkflow:
        environment_values = os.environ if environment is None else environment
        data = self.data
        secret_values: dict[str, str] = {}

        for raw_validation in self.config.get("validations", []):
            if not isinstance(raw_validation, dict):
                raise ValueError("Each workflow validation must be an object")
            condition = _require_text(
                raw_validation.get("assert"), "Workflow validation assertion"
            )
            when = raw_validation.get("when")
            if when is not None and not _matches(
                _require_text(when, "Workflow validation condition"), data
            ):
                continue
            if not _matches(condition, data):
                message = raw_validation.get("message", condition)
                raise ValueError(f"Workflow validation failed: {message}")

        def render(value: str) -> str:
            def replace_reference(match: re.Match[str]) -> str:
                kind = match.group("kind")
                key = match.group("colon") or match.group("dot") or ""
                if kind == "ENV":
                    resolved = environment_values.get(key)
                    if resolved is None:
                        raise ValueError(
                            f"Workflow requires environment variable {key}"
                        )
                    secret_values[resolved] = f"${{ENV:{key}}}"
                    return resolved
                resolved_data = _lookup_data(data, key)
                if resolved_data is None:
                    raise ValueError(f"Workflow template value data.{key} is missing")
                rendered_data = _stringify(resolved_data)
                return _REFERENCE.sub(replace_reference, rendered_data)

            return _REFERENCE.sub(replace_reference, value)

        def parameterize(value: str) -> str:
            def replace_reference(match: re.Match[str]) -> str:
                kind = match.group("kind")
                key = match.group("colon") or match.group("dot") or ""
                if kind == "ENV":
                    return f"${{ENV:{key}}}"
                _lookup_required_data(data, key)
                return f"${{INPUT:{key}}}"

            return _REFERENCE.sub(replace_reference, value)

        steps: list[str] = []
        for item in self.config["steps"]:
            if isinstance(item, str):
                steps.append(_require_text(item, "Workflow step"))
                continue
            if not isinstance(item, dict):
                raise ValueError("Workflow step must be text or a conditional object")
            condition = _require_text(
                item.get("when"), "Conditional workflow step condition"
            )
            if _matches(condition, data):
                steps.append(
                    _require_text(item.get("do"), "Conditional workflow step action")
                )
        memories: list[str] = []
        for raw_rule in self.config.get("rules", []):
            if not isinstance(raw_rule, dict):
                raise ValueError("Each workflow rule must be an object")
            condition = _require_text(raw_rule.get("when"), "Workflow rule condition")
            if not _matches(condition, data):
                continue
            memory = raw_rule.get("remember")
            if memory is not None:
                memories.append(render(_require_text(memory, "Workflow rule memory")))
            rule_steps = raw_rule.get("steps", [])
            if not isinstance(rule_steps, list):
                raise ValueError("Workflow rule steps must be a list")
            steps.extend(_require_text(item, "Workflow rule step") for item in rule_steps)

        notes = (self.text[: self.block_start] + self.text[self.block_end :]).strip()
        resolved_data = render_data_references(data, render)
        if not isinstance(resolved_data, dict):
            raise TypeError("Resolved workflow data must remain an object")
        return CompiledWorkflow(
            name=_require_text(self.config.get("name"), "Workflow name"),
            start_url=render(_require_text(self.config.get("start_url"), "Start URL")),
            goal=render(_require_text(self.config.get("goal"), "Workflow goal")),
            steps=tuple(render(step) for step in steps),
            memories=tuple(memories),
            reference_notes=notes,
            secret_values=secret_values,
            data=resolved_data,
            parameterized_start_url=parameterize(
                _require_text(self.config.get("start_url"), "Start URL")
            ),
            parameterized_goal=parameterize(
                _require_text(self.config.get("goal"), "Workflow goal")
            ),
            parameterized_steps=tuple(parameterize(step) for step in steps),
        )


class WorkflowDocumentStore:
    """Workspace-scoped storage used by CLI and Desktop form adapters."""

    def __init__(self, workspace: str | Path) -> None:
        self.workspace = Path(workspace).resolve()

    def load(self, path: str | Path) -> dict[str, Any]:
        source = self._resolve(path)
        spec = MarkdownWorkflowSpec.load(source)
        return self._document(source, spec)

    def update_data(
        self,
        path: str | Path,
        updates: Mapping[str, JsonValue],
    ) -> dict[str, Any]:
        source = self._resolve(path)
        spec = MarkdownWorkflowSpec.load(source).with_data(updates)
        spec.save(source)
        return self._document(source, spec)

    def _resolve(self, path: str | Path) -> Path:
        requested = Path(path)
        target = (
            requested.resolve()
            if requested.is_absolute()
            else (self.workspace / requested).resolve()
        )
        try:
            target.relative_to(self.workspace)
        except ValueError as exc:
            raise ValueError("Workflow document must stay inside the workspace") from exc
        if not target.is_file():
            raise FileNotFoundError(target)
        return target

    @staticmethod
    def _document(path: Path, spec: MarkdownWorkflowSpec) -> dict[str, Any]:
        return {
            "path": str(path),
            "markdown": spec.text,
            "name": str(spec.config.get("name", "")),
            "start_url": str(spec.config.get("start_url", "")),
            "goal": str(spec.config.get("goal", "")),
            "data": spec.data,
        }


def _lookup_data(data: Mapping[str, JsonValue], path: str) -> JsonValue:
    current: JsonValue = cast(dict[str, JsonValue], data)
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return None
    return current


def _lookup_required_data(data: Mapping[str, JsonValue], path: str) -> JsonValue:
    current: JsonValue = cast(dict[str, JsonValue], data)
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ValueError(f"Workflow template value data.{path} is missing")
    return current


def render_data_references(
    value: JsonValue,
    render: Callable[[str], str],
) -> JsonValue:
    """Resolve nested references in authoring inputs without changing their shape."""
    if isinstance(value, str):
        return render(value)
    if isinstance(value, list):
        return [render_data_references(item, render) for item in value]
    if isinstance(value, dict):
        return {
            key: render_data_references(item, render)
            for key, item in value.items()
        }
    return value


def _matches(expression: str, data: Mapping[str, JsonValue]) -> bool:
    conjunctions = [item.strip() for item in expression.split(" and ")]
    if len(conjunctions) > 1:
        return all(_matches(item, data) for item in conjunctions)
    match = _CONDITION.fullmatch(expression.strip())
    if match is None:
        raise ValueError(f"Unsupported workflow condition: {expression}")
    actual = _lookup_data(data, match.group("field"))
    expected = yaml.safe_load(match.group("expected"))
    operator = match.group("operator")
    if operator == "==":
        return bool(actual == expected)
    if operator == "!=":
        return bool(actual != expected)
    if not isinstance(expected, list):
        raise ValueError(f"Workflow condition {operator!r} requires a list")
    contained = actual in cast(list[JsonValue], expected)
    return not contained if operator == "not in" else contained


def _require_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be non-empty text")
    return value.strip()


def _stringify(value: JsonValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return str(
            yaml.safe_dump(value, allow_unicode=True, default_flow_style=True)
        ).strip()
    return str(value)


__all__ = [
    "CompiledWorkflow",
    "MarkdownWorkflowSpec",
    "WorkflowDocumentStore",
]
