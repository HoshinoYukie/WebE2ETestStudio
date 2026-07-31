from __future__ import annotations

import json
import threading

from web_e2e_test_studio.agent.control import (
    ActionExecutionContext,
    CancellationToken,
)
from web_e2e_test_studio.api.http import HttpActionExecutor, http_action_spec
from web_e2e_test_studio.domain import Action, ActionChannel, ActionStatus


class FakeResponse:
    status = 201
    headers = {"Content-Type": "application/json"}

    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *args) -> None:  # noqa: ANN002
        return None

    def read(self) -> bytes:
        return json.dumps({"id": 42}).encode()


def test_http_capability_is_self_describing_and_executes_structured_request() -> None:
    seen = []

    def open_request(request, timeout):  # noqa: ANN001
        seen.append((request, timeout))
        return FakeResponse()

    action = Action(
        name="http_request",
        channel=ActionChannel.API,
        arguments={
            "method": "POST",
            "url": "https://api.example.test/items",
            "json": {"name": "book"},
            "expected_status": 201,
            "timeout": 5,
        },
    )
    spec = http_action_spec()
    spec.validate(action)

    result = HttpActionExecutor(urlopen=open_request).execute(action)

    request, timeout = seen[0]
    assert spec.channel is ActionChannel.API
    assert request.full_url == "https://api.example.test/items"
    assert request.method == "POST"
    assert json.loads(request.data) == {"name": "book"}
    assert timeout == 5
    assert result.success
    assert result.output["status"] == 201
    assert result.output["json"] == {"id": 42}


def test_http_response_read_is_interrupted_by_cancellation() -> None:
    class BlockingResponse(FakeResponse):
        def __init__(self) -> None:
            self.reading = threading.Event()
            self.closed = threading.Event()

        def read(self) -> bytes:
            self.reading.set()
            self.closed.wait(2)
            return b"{}"

        def close(self) -> None:
            self.closed.set()

    response = BlockingResponse()
    token = CancellationToken()
    context = ActionExecutionContext.create(token=token, timeout_ms=5_000)
    outcome = []
    action = Action(
        "http_request",
        ActionChannel.API,
        arguments={
            "method": "GET",
            "url": "https://api.example.test/items",
        },
    )
    worker = threading.Thread(
        target=lambda: outcome.append(
            HttpActionExecutor(
                urlopen=lambda request, timeout: response
            ).execute(action, context)
        ),
        daemon=True,
    )
    worker.start()
    assert response.reading.wait(1)

    token.cancel("Stopped by user")
    worker.join(1)

    assert worker.is_alive() is False
    assert outcome[0].status is ActionStatus.CANCELLED
