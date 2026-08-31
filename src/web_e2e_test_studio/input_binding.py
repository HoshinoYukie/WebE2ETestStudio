"""Resolve portable runtime-input references without embedding product rules."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace

from web_e2e_test_studio.domain import Action, ElementTarget, JsonValue, Locator

_INPUT_REFERENCE = re.compile(r"\$\{INPUT:([A-Za-z_][A-Za-z0-9_.-]*)\}")


def resolve_action_inputs(
    action: Action,
    inputs: Mapping[str, JsonValue],
) -> Action:
    """Bind one action for execution while leaving its recorded form immutable."""
    arguments = resolve_input_references(action.arguments, inputs)
    if not isinstance(arguments, dict):
        raise TypeError("Resolved action arguments must remain an object")
    target = (
        _resolve_target(action.target, inputs)
        if action.target is not None
        else None
    )
    return replace(action, arguments=arguments, target=target)


def resolve_input_references(
    value: JsonValue,
    inputs: Mapping[str, JsonValue],
) -> JsonValue:
    """Resolve ``${INPUT:path}`` recursively, preserving exact-value types."""
    if isinstance(value, list):
        return [resolve_input_references(item, inputs) for item in value]
    if isinstance(value, dict):
        return {
            key: resolve_input_references(item, inputs)
            for key, item in value.items()
        }
    if not isinstance(value, str):
        return value
    exact = _INPUT_REFERENCE.fullmatch(value)
    if exact is not None:
        return _lookup(inputs, exact.group(1))

    def replace_reference(match: re.Match[str]) -> str:
        return _stringify(_lookup(inputs, match.group(1)))

    return _INPUT_REFERENCE.sub(replace_reference, value)


def _lookup(inputs: Mapping[str, JsonValue], path: str) -> JsonValue:
    current: JsonValue = dict(inputs)
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise ValueError(f"Runtime input {path!r} is missing")
    return current


def _resolve_target(
    target: ElementTarget,
    inputs: Mapping[str, JsonValue],
) -> ElementTarget:
    return replace(
        target,
        candidates=tuple(
            _resolve_locator(locator, inputs) for locator in target.candidates
        ),
    )


def _resolve_locator(
    locator: Locator,
    inputs: Mapping[str, JsonValue],
) -> Locator:
    value = resolve_input_references(locator.value, inputs)
    name = (
        resolve_input_references(locator.name, inputs)
        if locator.name is not None
        else None
    )
    if not isinstance(value, str) or (name is not None and not isinstance(name, str)):
        raise TypeError("Runtime locator inputs must resolve to strings")
    return replace(
        locator,
        value=value,
        name=name,
        fallbacks=tuple(
            _resolve_locator(fallback, inputs) for fallback in locator.fallbacks
        ),
    )


def _stringify(value: JsonValue) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


__all__ = ["resolve_action_inputs", "resolve_input_references"]
