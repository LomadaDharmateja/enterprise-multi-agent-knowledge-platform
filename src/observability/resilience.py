"""Retries, timeouts and circuit breakers for the external dependencies (M4 Task 3).

Four dependencies, four failure profiles. Each call is wrapped so that a slow or
failing dependency produces a typed, catchable exception within a stated bound rather
than an unbounded hang or a 500.

Three mechanisms, in the order they apply to a single call:

  timeout   -- a wall-clock bound on one attempt
  retry     -- exponential backoff with jitter, transient failures only
  breaker   -- after N consecutive failures, stop calling for a cooldown period

The breaker is what turns a dead dependency from "every request waits for the full
retry budget" into "every request fails immediately and the workflow degrades". That
is the difference the M4 exit criterion asks for.

**Honest limitation of the timeout.** A blocking call in a worker thread cannot be
killed from outside in CPython. `call_with_resilience` bounds how long the CALLER
waits, not how long the underlying socket operation runs; an abandoned thread may
continue until the driver's own timeout fires. Driver-level timeouts are therefore
configured as well where the client supports them, and the wall-clock guard is the
backstop that guarantees the bound the caller sees.
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any, Callable


class DependencyUnavailable(Exception):
    """Base: a dependency could not serve this call."""

    def __init__(self, dependency: str, message: str):
        self.dependency = dependency
        super().__init__(message)


class DependencyTimeout(DependencyUnavailable):
    """The call exceeded its wall-clock budget."""


class CircuitBreakerOpen(DependencyUnavailable):
    """The breaker is open; the call was not attempted."""


# --------------------------------------------------------------------------------
# Per-dependency profiles
# --------------------------------------------------------------------------------


@dataclass(frozen=True)
class DependencyProfile:
    name: str
    timeout_seconds: float
    max_retries: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 30.0
    failure_threshold: int = 3
    reset_after_seconds: float = 60.0


PROFILES: dict[str, DependencyProfile] = {
    "postgres": DependencyProfile("postgres", timeout_seconds=10.0),
    "neo4j": DependencyProfile("neo4j", timeout_seconds=10.0),
    "qdrant": DependencyProfile("qdrant", timeout_seconds=5.0),
    "gemini": DependencyProfile("gemini", timeout_seconds=30.0),
}


# --------------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------------


@dataclass
class CircuitBreaker:
    profile: DependencyProfile
    consecutive_failures: int = 0
    opened_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"

        if time.monotonic() - self.opened_at >= self.profile.reset_after_seconds:
            return "half_open"

        return "open"

    def allows_call(self) -> bool:
        return self.state != "open"

    def record_success(self) -> None:
        with self._lock:
            self.consecutive_failures = 0
            self.opened_at = None

    def record_failure(self) -> None:
        with self._lock:
            self.consecutive_failures += 1

            if self.consecutive_failures >= self.profile.failure_threshold:
                self.opened_at = time.monotonic()

    def reset(self) -> None:
        with self._lock:
            self.consecutive_failures = 0
            self.opened_at = None


_BREAKERS: dict[str, CircuitBreaker] = {
    name: CircuitBreaker(profile) for name, profile in PROFILES.items()
}
_BREAKER_LOCK = threading.Lock()


def breaker_for(dependency: str) -> CircuitBreaker:
    with _BREAKER_LOCK:
        if dependency not in _BREAKERS:
            profile = PROFILES.get(dependency) or DependencyProfile(dependency, 10.0)
            _BREAKERS[dependency] = CircuitBreaker(profile)

        return _BREAKERS[dependency]


def reset_all_breakers() -> None:
    for breaker in _BREAKERS.values():
        breaker.reset()


def breaker_states() -> dict[str, dict[str, Any]]:
    return {
        name: {
            "state": breaker.state,
            "consecutive_failures": breaker.consecutive_failures,
        }
        for name, breaker in _BREAKERS.items()
    }


# --------------------------------------------------------------------------------
# Transience
# --------------------------------------------------------------------------------

TRANSIENT_MARKERS = (
    "timeout", "timed out", "connection refused", "connection reset",
    "temporarily unavailable", "name resolution", "broken pipe",
    "service unavailable", "503", "502", "504", "429", "resource_exhausted",
    "server closed the connection", "could not connect", "unable to connect",
    "defunct connection", "connection is closed", "eof detected",
)


def is_transient(exc: BaseException) -> bool:
    """Retry only what a retry could plausibly fix.

    A syntax error in a query is not transient; retrying it three times just spends
    the budget before failing the same way.
    """
    if isinstance(exc, (DependencyTimeout, TimeoutError)):
        return True

    if isinstance(exc, CircuitBreakerOpen):
        return False

    text = f"{type(exc).__name__}: {exc}".lower()

    return any(marker in text for marker in TRANSIENT_MARKERS)


def backoff_delay(profile: DependencyProfile, attempt: int) -> float:
    """Exponential backoff with full jitter.

    Full jitter rather than fixed backoff: several legs retrying in lockstep against
    a recovering dependency is how a recovery turns back into an outage.
    """
    ceiling = min(profile.max_delay_seconds, profile.base_delay_seconds * (2 ** attempt))

    return random.uniform(0.0, ceiling)


# --------------------------------------------------------------------------------
# The wrapper
# --------------------------------------------------------------------------------

_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="resilience")


def call_with_resilience(
    dependency: str,
    operation: str,
    func: Callable[[], Any],
    *,
    profile: DependencyProfile | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Any:
    """Run `func` under this dependency's timeout, retry and breaker policy."""
    profile = profile or PROFILES.get(dependency) or DependencyProfile(dependency, 10.0)
    breaker = breaker_for(dependency)

    if not breaker.allows_call():
        raise CircuitBreakerOpen(
            dependency,
            f"{dependency} circuit breaker is open after "
            f"{breaker.consecutive_failures} consecutive failures; "
            f"{operation} was not attempted. It will be retried after "
            f"{profile.reset_after_seconds:.0f}s.",
        )

    last_error: BaseException | None = None

    for attempt in range(profile.max_retries + 1):
        started = time.monotonic()
        future = _EXECUTOR.submit(func)

        try:
            result = future.result(timeout=profile.timeout_seconds)
        except FuturesTimeout:
            elapsed = time.monotonic() - started
            future.cancel()
            last_error = DependencyTimeout(
                dependency,
                f"{dependency}.{operation} exceeded its "
                f"{profile.timeout_seconds:.0f}s timeout (waited {elapsed:.1f}s).",
            )
        except BaseException as exc:  # noqa: BLE001 -- classified below
            last_error = exc

            if not is_transient(exc):
                breaker.record_failure()
                raise
        else:
            breaker.record_success()
            return result

        if attempt < profile.max_retries:
            sleep(backoff_delay(profile, attempt))

    breaker.record_failure()

    if isinstance(last_error, DependencyUnavailable):
        raise last_error

    raise DependencyUnavailable(
        dependency,
        f"{dependency}.{operation} failed after {profile.max_retries + 1} attempts: "
        f"{type(last_error).__name__}: {last_error}",
    )
