"""Deterministic assertions for JSON HTTP responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from web_e2e_test_studio.domain import JsonValue

SUPPORTED_OPERATORS = frozenset(
    {"equals", "not_equals", "contains", "exists", "not_exists"}
)
_MISSING = object()


@dataclass(frozen=True)
class ResponseAssertion:
    """A validated assertion against a JSON path."""

    path: str
    operator: str
    expected: JsonValue = None


def parse_response_assertions(value: JsonValue) -> list[ResponseAssertion]:
    """Validate and normalize assertions stored in an HTTP action."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("HTTP response assertions must be a list")
    parsed: list[ResponseAssertion] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"HTTP response assertion {index} must be an object")
        path = item.get("path")
        operator = item.get("operator")
        if not isinstance(path, str) or not path.strip():
            raise ValueError(f"HTTP response assertion {index} requires a JSON path")
        if not isinstance(operator, str) or operator not in SUPPORTED_OPERATORS:
            raise ValueError(f"HTTP response assertion {index} has an unsupported operator")
        if operator not in {"exists", "not_exists"} and "expected" not in item:
            raise ValueError(f"HTTP response assertion {index} requires an expected value")
        parsed.append(ResponseAssertion(path.strip(), operator, item.get("expected")))
    return parsed


def assert_json_response(payload: JsonValue, assertions: JsonValue) -> None:
    """Raise ``AssertionError`` when a response assertion does not match."""
    for assertion in parse_response_assertions(assertions):
        actual = _resolve_path(payload, assertion.path)
        matches = _matches(actual, assertion.operator, assertion.expected)
        if not matches:
            actual_text = "<missing>" if actual is _MISSING else repr(actual)
            raise AssertionError(
                f"JSON response assertion failed at {assertion.path!r}: "
                f"{assertion.operator} {assertion.expected!r}; actual={actual_text}"
            )


def _resolve_path(payload: JsonValue, path: str) -> Any:
    current: Any = payload
    for segment in path.split("."):
        if isinstance(current, dict) and segment in current:
            current = current[segment]
        elif isinstance(current, list) and segment.isdigit():
            index = int(segment)
            if index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _matches(actual: Any, operator: str, expected: JsonValue) -> bool:
    if operator == "exists":
        return actual is not _MISSING
    if operator == "not_exists":
        return actual is _MISSING
    if actual is _MISSING:
        return False
    equal = type(actual) is type(expected) and actual == expected
    if operator == "equals":
        return equal
    if operator == "not_equals":
        return not equal
    if operator == "contains":
        if isinstance(actual, str) and isinstance(expected, str):
            return expected in actual
        if isinstance(actual, list):
            return any(type(item) is type(expected) and item == expected for item in actual)
        if isinstance(actual, dict) and isinstance(expected, str):
            return expected in actual
        return False
    raise AssertionError(f"Unsupported response assertion operator: {operator}")


__all__ = [
    "ResponseAssertion",
    "SUPPORTED_OPERATORS",
    "assert_json_response",
    "parse_response_assertions",
]
