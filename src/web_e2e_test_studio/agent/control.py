"""Thread-safe cancellation and deadline context for one action execution."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass

from web_e2e_test_studio.domain import ActionResult, ActionStatus


class ActionCancelledError(RuntimeError):
    """Raised when cooperative execution has been cancelled."""


class ActionTimeoutError(TimeoutError):
    """Raised when an action reaches its monotonic deadline."""


class CancellationToken:
    """A cooperative, thread-safe cancellation signal shared by a run."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._reason = "Cancelled"
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._next_callback_id = 0

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str:
        with self._lock:
            return self._reason

    def cancel(self, reason: str = "Cancelled by user") -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason.strip() or "Cancelled by user"
            self._event.set()
            callbacks = tuple(self._callbacks.values())
            self._callbacks.clear()
        for callback in callbacks:
            try:
                callback()
            except Exception:
                continue
        return True

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise ActionCancelledError(self.reason)

    def add_cancel_callback(
        self, callback: Callable[[], None]
    ) -> Callable[[], None]:
        with self._lock:
            if self._event.is_set():
                callback_id = -1
                run_now = True
            else:
                callback_id = self._next_callback_id
                self._next_callback_id += 1
                self._callbacks[callback_id] = callback
                run_now = False
        if run_now:
            with suppress(Exception):
                callback()

        def remove() -> None:
            with self._lock:
                self._callbacks.pop(callback_id, None)

        return remove


@dataclass(frozen=True)
class ActionExecutionContext:
    """Cancellation signal and monotonic deadline for one action."""

    cancellation: CancellationToken
    deadline: float | None = None
    timeout_seconds: float | None = None
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def create(
        cls,
        cancellation: CancellationToken | None = None,
        timeout_seconds: float | None = None,
        *,
        token: CancellationToken | None = None,
        timeout_ms: int | float | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> ActionExecutionContext:
        cancellation = token or cancellation or CancellationToken()
        if timeout_ms is not None:
            timeout_seconds = float(timeout_ms) / 1000
        if timeout_seconds is not None and timeout_seconds <= 0:
            raise ValueError("Action timeout must be greater than zero")
        deadline = (
            clock() + timeout_seconds
            if timeout_seconds is not None
            else None
        )
        return cls(cancellation, deadline, timeout_seconds, clock)

    @property
    def token(self) -> CancellationToken:
        return self.cancellation

    @property
    def cancelled(self) -> bool:
        return self.cancellation.cancelled

    @property
    def timed_out(self) -> bool:
        return self.deadline is not None and self.clock() >= self.deadline

    def remaining_seconds(self, default: float | None = None) -> float | None:
        if self.deadline is None:
            return default
        remaining = max(0.0, self.deadline - self.clock())
        return min(default, remaining) if default is not None else remaining

    def raise_if_stopped(self) -> None:
        self.cancellation.raise_if_cancelled()
        if self.timed_out:
            milliseconds = (
                self.timeout_seconds * 1000
                if self.timeout_seconds is not None
                else 0
            )
            raise ActionTimeoutError(
                f"Action timed out after {milliseconds:g} ms"
            )

    def cancelled_result(self) -> ActionResult:
        return ActionResult(
            ActionStatus.CANCELLED,
            error=self.cancellation.reason,
            error_category="cancelled",
        )

    def timeout_result(self, message: str = "Action timed out") -> ActionResult:
        return ActionResult(
            ActionStatus.TIMEOUT,
            error=message,
            error_category="timeout",
        )


__all__ = [
    "ActionCancelledError",
    "ActionExecutionContext",
    "ActionTimeoutError",
    "CancellationToken",
]
