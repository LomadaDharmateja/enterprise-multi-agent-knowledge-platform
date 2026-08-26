"""Per-call token and latency accounting for the Gemini agents.

M3 needs cost and latency attributable to each agent call, and nothing in the pipeline
recorded either -- `generate_content` was called in three places and the response's
`usage_metadata` was discarded every time.

This is deliberately a thin recorder, not the full cost/latency work M5 owns. It
captures what a measurement run needs: which agent, how many input and output tokens,
and how long the call took. Aggregation, pricing and percentiles are M5's.

The recorder is process-local and opt-in: nothing accumulates unless a caller opens a
`collect()` scope, so ordinary runs are unaffected.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Any

_local = threading.local()

# Opt-in client-side pacing. Off unless LLM_MIN_INTERVAL_SECONDS is set, so ordinary
# product behaviour is unchanged. The evaluation runner sets it because the Gemini
# free tier allows 15 requests/minute and an unpaced 83-item run bursts past that --
# which is a property of the measurement harness, not of the system under test.
_rate_lock = threading.Lock()
_last_call_at = [0.0]


def _min_interval_seconds() -> float:
    try:
        return float(os.getenv("LLM_MIN_INTERVAL_SECONDS", "0") or 0)
    except ValueError:
        return 0.0


def throttle() -> None:
    interval = _min_interval_seconds()

    if interval <= 0:
        return

    with _rate_lock:
        wait = _last_call_at[0] + interval - time.monotonic()

        if wait > 0:
            time.sleep(wait)

        _last_call_at[0] = time.monotonic()


def _sink() -> list[dict[str, Any]] | None:
    return getattr(_local, "sink", None)


@contextmanager
def collect():
    """Collect every LLM call made inside this scope.

    Nested scopes are not supported; the inner scope wins and the outer resumes
    afterwards, which is enough for a single-threaded evaluation runner.
    """
    previous = _sink()
    _local.sink = []

    try:
        yield _local.sink
    finally:
        _local.sink = previous


def extract_usage(response: Any) -> dict[str, int | None]:
    """Pull token counts off a google-genai response, tolerating their absence.

    A missing `usage_metadata` is recorded as None rather than 0. Zero is a
    measurement; None means the provider did not tell us, and reporting one as the
    other is how a cost table quietly becomes fiction.
    """
    metadata = getattr(response, "usage_metadata", None)

    if metadata is None:
        return {"input_tokens": None, "output_tokens": None, "total_tokens": None}

    def get(*names):
        for name in names:
            value = getattr(metadata, name, None)

            if value is not None:
                return int(value)

        return None

    return {
        "input_tokens": get("prompt_token_count", "input_token_count"),
        "output_tokens": get("candidates_token_count", "output_token_count"),
        "total_tokens": get("total_token_count"),
    }


def record_call(
    agent: str,
    model: str,
    response: Any,
    latency_ms: float,
    prompt_chars: int | None = None,
) -> dict[str, Any]:
    entry = {
        "agent": agent,
        "model": model,
        "latency_ms": round(latency_ms, 1),
        "prompt_chars": prompt_chars,
        **extract_usage(response),
    }

    sink = _sink()

    if sink is not None:
        sink.append(entry)

    return entry


@contextmanager
def timed_call(agent: str, model: str, prompt_chars: int | None = None):
    """Time a Gemini call and record its usage.

    Usage:
        with timed_call("answer_agent", model, len(prompt)) as call:
            response = client.models.generate_content(...)
            call["response"] = response
    """
    throttle()

    started = time.perf_counter()
    holder: dict[str, Any] = {"response": None}

    try:
        yield holder
    finally:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        record_call(
            agent=agent,
            model=model,
            response=holder.get("response"),
            latency_ms=elapsed_ms,
            prompt_chars=prompt_chars,
        )
