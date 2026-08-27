from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from threading import Lock
from typing import TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Production-safe three-state circuit breaker.

    States:
    - CLOSED: calls pass through; consecutive failures are counted.
    - OPEN: calls fail fast until ``reset_timeout_seconds`` has elapsed.
    - HALF_OPEN: a limited number of probe calls are allowed; ``success_threshold``
      consecutive successes close the circuit, a single failure re-opens it.

    All state mutations are guarded by ``_lock`` so several worker threads can share
    one breaker (see the ``concurrent_load`` chaos scenario). The lock deliberately
    does NOT cover the wrapped call itself -- holding it across provider I/O would
    serialise every request and defeat the point of the concurrency test.
    """

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, repr=False, compare=False)

    def allow_request(self) -> bool:
        """Return whether a request should be attempted.

        CLOSED and HALF_OPEN always allow (HALF_OPEN lets a probe through). OPEN
        denies until ``reset_timeout_seconds`` has elapsed since ``opened_at``, at
        which point the breaker moves to HALF_OPEN and admits one probe.
        """
        with self._lock:
            if self.state is CircuitState.CLOSED:
                return True
            if self.state is CircuitState.HALF_OPEN:
                return True

            # OPEN: fail fast until the reset timeout expires.
            if self.opened_at is None:
                # Defensive: an externally forced OPEN state without a timestamp.
                self.opened_at = time.monotonic()
                return False
            if time.monotonic() - self.opened_at >= self.reset_timeout_seconds:
                self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
                self.success_count = 0
                return True
            return False

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        """Call ``fn`` through the breaker, recording the outcome.

        Raises ``CircuitOpenError`` without invoking ``fn`` when the circuit is open,
        which is what keeps a failing provider from being hammered (no retry storm).
        """
        if not self.allow_request():
            raise CircuitOpenError(f"circuit '{self.name}' is open")
        try:
            result = fn(*args, **kwargs)
        except Exception:
            self.record_failure()
            raise
        self.record_success()
        return result

    def record_success(self) -> None:
        """Record a successful call and close the circuit once probes are satisfied."""
        with self._lock:
            self.failure_count = 0
            self.success_count += 1
            if (
                self.state is CircuitState.HALF_OPEN
                and self.success_count >= self.success_threshold
            ):
                self._transition(CircuitState.CLOSED, "probe_success")
                self.success_count = 0
                self.opened_at = None

    def record_failure(self) -> None:
        """Record a failed call and open the circuit when warranted.

        The HALF_OPEN case and the threshold case are handled separately (if/elif) so
        each records its own reason: a failed probe is ``probe_failure`` regardless of
        ``failure_count``, while accumulating failures in CLOSED yield
        ``failure_threshold_reached``.
        """
        with self._lock:
            self.failure_count += 1
            self.success_count = 0

            previous_state = self.state
            if self.state is CircuitState.HALF_OPEN:
                self._transition(CircuitState.OPEN, "probe_failure")
            elif self.failure_count >= self.failure_threshold:
                self._transition(CircuitState.OPEN, "failure_threshold_reached")

            # Only stamp opened_at on a real CLOSED/HALF_OPEN -> OPEN edge. Stamping it
            # unconditionally would restart the reset timer on every failure recorded
            # while already OPEN, so the breaker could never reach HALF_OPEN.
            if self.state is not previous_state and self.state is CircuitState.OPEN:
                self.opened_at = time.monotonic()

    def snapshot(self) -> dict[str, object]:
        """Return a JSON-serialisable view of the breaker, for metrics and reports."""
        with self._lock:
            return {
                "name": self.name,
                "state": self.state.value,
                "failure_count": self.failure_count,
                "success_count": self.success_count,
                "failure_threshold": self.failure_threshold,
                "reset_timeout_seconds": self.reset_timeout_seconds,
                "success_threshold": self.success_threshold,
                "transitions": len(self.transition_log),
                "transition_log": list(self.transition_log),
            }

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        if self.state == new_state:
            return
        self.transition_log.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        )
        self.state = new_state
