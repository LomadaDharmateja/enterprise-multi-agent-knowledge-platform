"""Timeouts, retries and circuit breakers per dependency (M4 Task 3).

No live dependency is needed: the policy is what is under test, and a policy that only
works when a real database happens to be slow is not testable. Each test drives the
wrapper with a controlled function and asserts the stated bound.
"""

from __future__ import annotations

import time

import pytest

from resilience import (
    PROFILES,
    CircuitBreaker,
    CircuitBreakerOpen,
    DependencyProfile,
    DependencyTimeout,
    DependencyUnavailable,
    backoff_delay,
    breaker_for,
    call_with_resilience,
    is_transient,
    reset_all_breakers,
)


@pytest.fixture(autouse=True)
def clean_breakers():
    reset_all_breakers()
    yield
    reset_all_breakers()


def no_sleep(_seconds: float) -> None:
    """Collapse backoff so retry behaviour is testable without waiting for it."""


# --------------------------------------------------------------------------------
# Stated timeout bounds, one test per dependency
# --------------------------------------------------------------------------------


def test_the_four_dependencies_have_the_stated_timeouts():
    assert PROFILES["postgres"].timeout_seconds == 10.0
    assert PROFILES["neo4j"].timeout_seconds == 10.0
    assert PROFILES["qdrant"].timeout_seconds == 5.0
    assert PROFILES["gemini"].timeout_seconds == 30.0


@pytest.mark.parametrize("dependency", ["postgres", "neo4j", "qdrant", "gemini"])
def test_timeout_raises_dependency_timeout_within_its_bound(dependency):
    """A hanging call must fail as DependencyTimeout, not hang, and not return.

    The real profile timeouts are 5-30s, which would make this suite slow for no
    added confidence: what is under test is that the bound is enforced and the right
    exception is raised. The timeout is shortened and the bound asserted against it.
    """
    profile = DependencyProfile(
        dependency, timeout_seconds=0.25, max_retries=0, failure_threshold=99
    )

    def hangs():
        time.sleep(30)

    started = time.monotonic()

    with pytest.raises(DependencyTimeout) as caught:
        call_with_resilience(
            dependency, "hanging_call", hangs, profile=profile, sleep=no_sleep
        )

    elapsed = time.monotonic() - started

    assert caught.value.dependency == dependency
    assert "timeout" in str(caught.value).lower()
    assert elapsed < 3.0, f"caller waited {elapsed:.1f}s for a 0.25s timeout"


def test_a_fast_call_returns_normally_and_closes_the_breaker():
    breaker = breaker_for("postgres")
    breaker.consecutive_failures = 2

    assert call_with_resilience("postgres", "ok", lambda: "value") == "value"
    assert breaker.consecutive_failures == 0
    assert breaker.state == "closed"


# --------------------------------------------------------------------------------
# Retry
# --------------------------------------------------------------------------------


def test_transient_failures_are_retried_then_succeed():
    attempts = {"n": 0}

    def flaky():
        attempts["n"] += 1

        if attempts["n"] < 3:
            raise ConnectionError("connection refused")

        return "recovered"

    profile = DependencyProfile("postgres", timeout_seconds=5.0, max_retries=3)
    result = call_with_resilience(
        "postgres", "flaky", flaky, profile=profile, sleep=no_sleep
    )

    assert result == "recovered"
    assert attempts["n"] == 3


def test_a_non_transient_failure_is_not_retried():
    """Retrying a syntax error spends the budget and fails the same way."""
    attempts = {"n": 0}

    def broken():
        attempts["n"] += 1
        raise ValueError("syntax error at or near SELECT")

    with pytest.raises(ValueError):
        call_with_resilience("postgres", "broken", broken, sleep=no_sleep)

    assert attempts["n"] == 1


def test_retries_are_capped_and_then_raise():
    attempts = {"n": 0}

    def always_fails():
        attempts["n"] += 1
        raise ConnectionError("connection refused")

    profile = DependencyProfile("qdrant", timeout_seconds=5.0, max_retries=3)

    with pytest.raises(DependencyUnavailable):
        call_with_resilience(
            "qdrant", "always_fails", always_fails, profile=profile, sleep=no_sleep
        )

    assert attempts["n"] == 4, "expected the initial attempt plus 3 retries"


@pytest.mark.parametrize(
    "message, transient",
    [
        ("connection refused", True),
        ("Temporary failure in name resolution", True),
        ("429 RESOURCE_EXHAUSTED", True),
        ("503 Service Unavailable", True),
        ("syntax error at or near", False),
        ("relation does not exist", False),
    ],
)
def test_transience_classification(message, transient):
    assert is_transient(RuntimeError(message)) is transient


def test_backoff_grows_and_is_bounded_and_jittered():
    profile = DependencyProfile("gemini", timeout_seconds=30.0, base_delay_seconds=1.0)

    for attempt in range(6):
        samples = [backoff_delay(profile, attempt) for _ in range(50)]
        assert all(0.0 <= s <= profile.max_delay_seconds for s in samples)
        assert len(set(samples)) > 1, "full jitter should not produce a constant delay"

    assert max(backoff_delay(profile, 10) for _ in range(200)) <= profile.max_delay_seconds


# --------------------------------------------------------------------------------
# Circuit breaker
# --------------------------------------------------------------------------------


def test_breaker_opens_after_three_consecutive_failures():
    profile = DependencyProfile("neo4j", timeout_seconds=1.0, max_retries=0)

    def fails():
        raise ConnectionError("connection refused")

    for _ in range(3):
        with pytest.raises(DependencyUnavailable):
            call_with_resilience("neo4j", "fails", fails, profile=profile, sleep=no_sleep)

    assert breaker_for("neo4j").state == "open"


def test_an_open_breaker_fails_fast_without_calling_the_dependency():
    """The point of the breaker: a dead dependency stops costing the retry budget."""
    called = {"n": 0}

    def counts():
        called["n"] += 1
        raise ConnectionError("connection refused")

    profile = DependencyProfile("neo4j", timeout_seconds=1.0, max_retries=0)

    for _ in range(3):
        with pytest.raises(DependencyUnavailable):
            call_with_resilience("neo4j", "c", counts, profile=profile, sleep=no_sleep)

    before = called["n"]

    with pytest.raises(CircuitBreakerOpen):
        call_with_resilience("neo4j", "c", counts, profile=profile, sleep=no_sleep)

    assert called["n"] == before, "an open breaker must not call the dependency"


def test_breaker_half_opens_after_the_reset_window():
    profile = DependencyProfile(
        "qdrant", timeout_seconds=1.0, failure_threshold=3, reset_after_seconds=60.0
    )
    breaker = CircuitBreaker(profile)

    for _ in range(3):
        breaker.record_failure()

    assert breaker.state == "open"

    # Rewind the clock rather than sleeping 60 seconds.
    breaker.opened_at = time.monotonic() - 61.0

    assert breaker.state == "half_open"
    assert breaker.allows_call()

    breaker.record_success()
    assert breaker.state == "closed"


def test_breakers_are_isolated_per_dependency():
    profile = DependencyProfile("postgres", timeout_seconds=1.0, max_retries=0)

    def fails():
        raise ConnectionError("connection refused")

    for _ in range(3):
        with pytest.raises(DependencyUnavailable):
            call_with_resilience(
                "postgres", "f", fails, profile=profile, sleep=no_sleep
            )

    assert breaker_for("postgres").state == "open"
    assert breaker_for("neo4j").state == "closed"
    assert breaker_for("qdrant").state == "closed"
    assert breaker_for("gemini").state == "closed"
