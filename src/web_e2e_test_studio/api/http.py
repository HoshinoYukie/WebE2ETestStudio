"""Deterministic HTTP capability using only the Python standard library."""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import suppress
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from web_e2e_test_studio.agent import ActionSpec
from web_e2e_test_studio.agent.control import ActionExecutionContext
from web_e2e_test_studio.domain import (
    Action,
    ActionChannel,
    ActionResult,
    JsonValue,
)


def http_action_spec() -> ActionSpec:
    """Return the model- and UI-facing contract for HTTP requests."""
    return ActionSpec(
        name="http_request",
        channel=ActionChannel.API,
        description=(
            "Send an HTTP request and verify its status. Supports method, url, "
            "headers, json or body, expected_status, and timeout."
        ),
        required_arguments=("method", "url"),
        argument_schema={
            "method": "string",
            "url": "string",
            "headers": "object",
            "json": "json",
            "body": "string",
            "expected_status": "integer",
            "timeout": "number",
        },
    )


class HttpActionExecutor:
    """Execute a structured HTTP action without shell or model access."""

    def __init__(
        self,
        *,
        urlopen: Callable[..., Any] = urlopen,
    ) -> None:
        self._urlopen = urlopen

    def execute(
        self,
        action: Action,
        control: ActionExecutionContext | None = None,
    ) -> ActionResult:
        def remove_cancel_callback() -> None:
            return None

        try:
            if control is not None:
                control.raise_if_stopped()
            method = _required_string(action, "method").upper()
            url = _required_string(action, "url")
            headers = _headers(action.arguments.get("headers"))
            data = _request_data(action, headers)
            timeout = _number(action.arguments.get("timeout", 30), "timeout")
            if control is not None:
                remaining = control.remaining_seconds(timeout)
                if remaining is not None and remaining <= 0:
                    return control.timeout_result("HTTP action timed out")
                if remaining is not None:
                    timeout = remaining
            expected = int(
                _number(
                    action.arguments.get("expected_status", 200),
                    "expected_status",
                )
            )
            request = Request(
                url=url,
                data=data,
                headers=headers,
                method=method,
            )
            try:
                response_context = self._urlopen(request, timeout=timeout)
            except HTTPError as error:
                response_context = error
            close_response = getattr(response_context, "close", None)
            if control is not None and callable(close_response):
                remove_cancel_callback = (
                    control.cancellation.add_cancel_callback(close_response)
                )
            with response_context as response:
                raw = response.read()
                if control is not None:
                    control.raise_if_stopped()
                status = int(response.status)
                output: dict[str, JsonValue] = {
                    "status": status,
                    "headers": {
                        str(key): str(value)
                        for key, value in response.headers.items()
                    },
                    "body": raw.decode("utf-8", errors="replace"),
                }
                with suppress(json.JSONDecodeError, UnicodeDecodeError):
                    output["json"] = cast(JsonValue, json.loads(raw))
                if status != expected:
                    return ActionResult.failed(
                        f"Expected HTTP {expected}, received {status}"
                    )
                return ActionResult.succeeded(output)
        except Exception as exc:
            if control is not None and control.cancelled:
                return control.cancelled_result()
            if control is not None and control.timed_out:
                return control.timeout_result("HTTP action timed out")
            return ActionResult.failed(str(exc))
        finally:
            remove_cancel_callback()


def _required_string(action: Action, key: str) -> str:
    value = action.arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"http_request requires non-empty string {key!r}")
    return value


def _headers(value: JsonValue) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("http_request headers must be an object")
    return {str(key): str(item) for key, item in value.items()}


def _request_data(action: Action, headers: dict[str, str]) -> bytes | None:
    if "json" in action.arguments:
        headers.setdefault("Content-Type", "application/json")
        return json.dumps(
            action.arguments["json"],
            ensure_ascii=False,
        ).encode("utf-8")
    body = action.arguments.get("body")
    if body is None:
        return None
    if not isinstance(body, str):
        raise ValueError("http_request body must be a string")
    return body.encode("utf-8")


def _number(value: JsonValue, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"http_request {name} must be numeric")
    return float(value)


__all__ = ["HttpActionExecutor", "http_action_spec"]
