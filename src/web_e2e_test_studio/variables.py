"""Portable generated variables shared by Studio runtime and PyTest export."""

from __future__ import annotations

import random
import re
import string
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, cast

from web_e2e_test_studio.domain import JsonValue

VARIABLE_REFERENCE = re.compile(r"\$\{VAR:([A-Za-z_][A-Za-z0-9_]*)\}")
_VARIABLE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SUPPORTED_KINDS = {"uuid", "timestamp", "datetime", "integer", "digits", "alphanumeric", "email"}


@dataclass(frozen=True)
class VariableDefinition:
    name: str
    description: str
    kind: str
    options: dict[str, JsonValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not _VARIABLE_NAME.fullmatch(self.name):
            raise ValueError(
                "Variable name must start with a letter or underscore and "
                "contain only letters, digits, and underscores"
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "name": self.name,
            "description": self.description,
            "kind": self.kind,
            "options": self.options,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> VariableDefinition:
        options = value.get("options", {})
        if not isinstance(options, dict):
            raise ValueError("Variable options must be an object")
        kind = str(value["kind"])
        normalized = cast(dict[str, JsonValue], dict(options))
        if kind not in _SUPPORTED_KINDS:
            raise ValueError(f"Unsupported variable generator kind: {kind!r}")
        _validate_rule_options(kind, normalized)
        return cls(
            name=str(value["name"]),
            description=str(value.get("description", "")),
            kind=kind,
            options=normalized,
        )


class VariableRuleBackend(Protocol):
    def complete_json(
        self, system_prompt: str, user_prompt: str, screenshot_path: str
    ) -> dict[str, Any]: ...


_VARIABLE_RULE_SYSTEM_PROMPT = """You convert a variable format description
written in any language into one JSON generator rule. Return only
{"kind": string, "options": object}. Supported rules:
- uuid: {}
- timestamp: {"unit": "seconds"|"milliseconds"}
- datetime: {"format": strftime string}
- integer: {"minimum": integer, "maximum": integer}
- digits or alphanumeric: {"length": 1..256, "prefix": string}
- email: {"length": 1..256, "domain": string}
Preserve literal prefixes and domains exactly. Do not translate values or add
fields."""


class VLMVariableDefinitionCompiler:
    """Compile a description in any language into a portable generator rule."""

    def __init__(self, backend: VariableRuleBackend) -> None:
        self._backend = backend

    def compile(self, name: str, description: str) -> VariableDefinition:
        clean_name, clean_description = name.strip(), description.strip()
        if not clean_description:
            raise ValueError("Variable format description is required")
        rule = self._backend.complete_json(
            _VARIABLE_RULE_SYSTEM_PROMPT,
            f"Variable name: {clean_name}\nFormat description: {clean_description}",
            "",
        )
        return variable_definition_from_rule(clean_name, clean_description, rule)


def compile_variable_definition(
    name: str, description: str, backend: VariableRuleBackend
) -> VariableDefinition:
    return VLMVariableDefinitionCompiler(backend).compile(name, description)


def variable_definition_from_rule(
    name: str, description: str, rule: dict[str, Any]
) -> VariableDefinition:
    """Validate a model-produced rule without interpreting human language."""
    kind, options = rule.get("kind"), rule.get("options", {})
    if kind not in _SUPPORTED_KINDS:
        raise ValueError(f"Unsupported variable generator kind: {kind!r}")
    if not isinstance(options, dict):
        raise ValueError("Variable rule options must be an object")
    normalized = cast(dict[str, JsonValue], dict(options))
    _validate_rule_options(str(kind), normalized)
    return VariableDefinition(name.strip(), description.strip(), str(kind), normalized)


def _validate_rule_options(kind: str, options: dict[str, JsonValue]) -> None:
    allowed = {
        "uuid": set(),
        "timestamp": {"unit"},
        "datetime": {"format"},
        "integer": {"minimum", "maximum"},
        "digits": {"length", "prefix"},
        "alphanumeric": {"length", "prefix"},
        "email": {"length", "domain"},
    }
    unexpected = set(options) - allowed[kind]
    if unexpected:
        raise ValueError(f"Unsupported variable options: {sorted(unexpected)}")
    if kind == "timestamp" and options.get("unit", "seconds") not in {"seconds", "milliseconds"}:
        raise ValueError("Timestamp unit must be seconds or milliseconds")
    if kind == "datetime":
        value = options.get("format", "%Y-%m-%d")
        if not isinstance(value, str) or not value or len(value) > 100:
            raise ValueError("Datetime format must be a non-empty string")
    if kind == "integer" and _integer_option(options, "minimum") > _integer_option(
        options, "maximum"
    ):
        raise ValueError("Variable integer range minimum exceeds maximum")
    if kind in {"digits", "alphanumeric", "email"}:
        length = _integer_option(options, "length", 12)
        if not 1 <= length <= 256:
            raise ValueError("Variable length must be between 1 and 256")
    if kind in {"digits", "alphanumeric"} and not isinstance(options.get("prefix", ""), str):
        raise ValueError("Variable prefix must be a string")
    if kind == "email":
        domain = options.get("domain", "example.test")
        if not isinstance(domain, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", domain):
            raise ValueError("Email domain is invalid")


def generate_variable(definition: VariableDefinition) -> str:
    options, generator = definition.options, random.SystemRandom()
    if definition.kind == "uuid":
        return str(uuid.uuid4())
    if definition.kind == "timestamp":
        value = datetime.now(UTC).timestamp()
        return str(int(value * (1000 if options.get("unit") == "milliseconds" else 1)))
    if definition.kind == "datetime":
        return datetime.now(UTC).strftime(str(options.get("format", "%Y-%m-%d")))
    if definition.kind == "integer":
        return str(
            generator.randint(
                _integer_option(options, "minimum"), _integer_option(options, "maximum")
            )
        )
    if definition.kind in {"digits", "alphanumeric"}:
        alphabet = (
            string.digits if definition.kind == "digits" else string.ascii_letters + string.digits
        )
        return str(options.get("prefix", "")) + "".join(
            generator.choice(alphabet) for _ in range(_integer_option(options, "length", 12))
        )
    if definition.kind == "email":
        local = "".join(
            generator.choice(string.ascii_lowercase + string.digits)
            for _ in range(_integer_option(options, "length", 12))
        )
        return f"{local}@{options.get('domain', 'example.test')}"
    raise ValueError(f"Unsupported variable generator kind: {definition.kind}")


class VariableRegistry:
    def __init__(
        self,
        definitions: tuple[VariableDefinition, ...] = (),
        *,
        definition_compiler: Callable[[str, str], VariableDefinition] | None = None,
    ) -> None:
        self._definitions = {item.name: item for item in definitions}
        self._values = {name: generate_variable(item) for name, item in self._definitions.items()}
        self._definition_compiler = definition_compiler

    def define(self, name: str, description: str) -> tuple[VariableDefinition, str]:
        if self._definition_compiler is None:
            raise RuntimeError("Variable definition compiler is not configured")
        definition = self._definition_compiler(name, description)
        value = generate_variable(definition)
        self._definitions[definition.name], self._values[definition.name] = definition, value
        return definition, value

    def definitions(self) -> tuple[VariableDefinition, ...]:
        return tuple(self._definitions.values())

    def values(self) -> dict[str, str]:
        return dict(self._values)

    def resolve(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            return VARIABLE_REFERENCE.sub(self._replace_reference, value)
        if isinstance(value, list):
            return [self.resolve(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self.resolve(item) for key, item in value.items()}
        return value

    def restore_references(self, value: JsonValue) -> JsonValue:
        if isinstance(value, str):
            for name, generated in sorted(
                self._values.items(), key=lambda item: len(item[1]), reverse=True
            ):
                if generated:
                    value = value.replace(generated, f"${{VAR:{name}}}")
            return value
        if isinstance(value, list):
            return [self.restore_references(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self.restore_references(item) for key, item in value.items()}
        return value

    def referenced_names(self, value: JsonValue) -> set[str]:
        if isinstance(value, str):
            return set(VARIABLE_REFERENCE.findall(value))
        if isinstance(value, list):
            return set().union(*(self.referenced_names(item) for item in value)) if value else set()
        if isinstance(value, dict):
            return (
                set().union(*(self.referenced_names(item) for item in value.values()))
                if value
                else set()
            )
        return set()

    def _replace_reference(self, match: re.Match[str]) -> str:
        try:
            return self._values[match.group(1)]
        except KeyError as exc:
            raise ValueError(f"Undefined generated variable: {match.group(1)}") from exc


def _integer_option(options: dict[str, JsonValue], key: str, default: int | None = None) -> int:
    value = options.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"Variable option {key!r} must be an integer")
    return int(value)


__all__ = [
    "VARIABLE_REFERENCE",
    "VariableDefinition",
    "VariableRegistry",
    "VLMVariableDefinitionCompiler",
    "compile_variable_definition",
    "generate_variable",
    "variable_definition_from_rule",
]
