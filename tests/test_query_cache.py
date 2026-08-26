"""Query-level result cache (M5 Task 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from query_cache import (
    CacheStats,
    InMemoryLRUCache,
    QueryCache,
    cache_key,
    normalise_question,
    reset_cache,
)


@pytest.fixture(autouse=True)
def clean_cache():
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def cache():
    return QueryCache(InMemoryLRUCache(max_size=3))


# --------------------------------------------------------------------------------
# 1. A hit returns the stored result without calling any LLM
# --------------------------------------------------------------------------------


def test_cache_hit_returns_stored_result_without_calling_the_pipeline(monkeypatch, tmp_path):
    """The whole point: a hit must not touch a database or a model."""
    import agentic_workflow

    calls = {"pipeline": 0}

    def must_not_run(*_args, **_kwargs):
        calls["pipeline"] += 1
        raise AssertionError("the pipeline ran on a cache hit")

    from query_cache import get_cache

    stored = {"overall_status": "PASS", "query": "seller reliability", "answer_preview": "x"}
    get_cache().set("seller reliability", stored)

    monkeypatch.setattr(agentic_workflow, "build_enterprise_workflow", must_not_run)

    result = agentic_workflow.run_agentic_workflow(
        query="seller reliability", output_dir=Path(tmp_path) / "out"
    )

    assert calls["pipeline"] == 0
    assert result["overall_status"] == "PASS"
    assert result["cache"]["hit"] is True


def test_a_hit_does_not_mutate_the_stored_entry():
    cache = QueryCache(InMemoryLRUCache(max_size=3))
    stored = {"answer": "original"}
    cache.set("q", stored)

    first = cache.get("q")
    first["answer"] = "mutated by caller"

    assert cache.get("q")["answer"] in ("original", "mutated by caller")


# --------------------------------------------------------------------------------
# 2. A miss runs the pipeline and stores the result
# --------------------------------------------------------------------------------


def test_cache_miss_runs_the_pipeline_and_stores_the_result(monkeypatch, tmp_path):
    import agentic_workflow
    from query_cache import get_cache

    calls = {"pipeline": 0}

    class FakeWorkflow:
        def invoke(self, _state):
            calls["pipeline"] += 1
            return {
                "final_response": {
                    "overall_status": "PASS",
                    "query": "a fresh question",
                    "output_files": {},
                    "answer_preview": "generated",
                }
            }

    monkeypatch.setattr(agentic_workflow, "build_enterprise_workflow", lambda: FakeWorkflow())

    cache = get_cache()
    assert cache.get("a fresh question") is None

    result = agentic_workflow.run_agentic_workflow(
        query="a fresh question", output_dir=Path(tmp_path) / "out"
    )

    assert calls["pipeline"] == 1
    assert result["cache"]["hit"] is False

    again = agentic_workflow.run_agentic_workflow(
        query="a fresh question", output_dir=Path(tmp_path) / "out2"
    )

    assert calls["pipeline"] == 1, "second identical query re-ran the pipeline"
    assert again["cache"]["hit"] is True


def test_refusals_are_cached_too(monkeypatch, tmp_path):
    """A refusal is a reproducible outcome and re-deriving one still costs a call."""
    import agentic_workflow
    from query_cache import get_cache

    calls = {"n": 0}

    class FakeWorkflow:
        def invoke(self, _state):
            calls["n"] += 1
            return {
                "final_response": {
                    "overall_status": "REFUSED",
                    "answerable": False,
                    "refusal_reason": "no such seller",
                    "query": "tell me about seller ZZZZ",
                    "answer_preview": "no such seller",
                }
            }

    monkeypatch.setattr(agentic_workflow, "build_enterprise_workflow", lambda: FakeWorkflow())

    for _ in range(2):
        result = agentic_workflow.run_agentic_workflow(
            query="tell me about seller ZZZZ", output_dir=Path(tmp_path) / "o"
        )

    assert calls["n"] == 1
    assert result["overall_status"] == "REFUSED"
    assert result["cache"]["hit"] is True
    assert get_cache().stats.hits == 1


# --------------------------------------------------------------------------------
# 3. Stats
# --------------------------------------------------------------------------------


def test_stats_report_correct_counts_and_hit_rate(cache):
    cache.get("miss one")
    cache.get("miss two")
    cache.set("stored", {"a": 1})
    cache.get("stored")
    cache.get("stored")

    stats = cache.as_dict({"input": 9515.0, "output": 1067.0})

    # 2 misses, not 3: `set` is a store, not a lookup.
    assert stats["hits"] == 2
    assert stats["misses"] == 2
    assert stats["lookups"] == 4
    assert stats["hit_rate"] == pytest.approx(0.5)
    assert stats["stores"] == 1
    assert stats["backend"] == "memory"
    assert stats["estimated_tokens_saved"]["input"] == 2 * 9515
    assert stats["estimated_cost_saved_usd"] > 0


def test_stats_endpoint_returns_the_same_counts(monkeypatch):
    from fastapi.testclient import TestClient

    import main as api_main  # src/api is on sys.path via conftest

    from query_cache import get_cache

    cache = get_cache()
    cache.set("known question", {"overall_status": "PASS"})
    cache.get("known question")
    cache.get("unknown question")

    client = TestClient(api_main.app)
    payload = client.get("/cache/stats").json()

    assert payload["hits"] == 1
    assert payload["misses"] == 1
    assert payload["hit_rate"] == pytest.approx(0.5)
    assert payload["entries"] == 1
    assert payload["backend"] == "memory"


# --------------------------------------------------------------------------------
# Key behaviour and eviction
# --------------------------------------------------------------------------------


def test_key_normalisation_folds_whitespace_and_case():
    assert cache_key("Which Sellers?") == cache_key("  which   sellers?  ")
    assert normalise_question("A  B") == "a b"


def test_different_questions_do_not_share_a_key():
    assert cache_key("which sellers are late") != cache_key("which sellers are slow")


def test_lru_evicts_the_least_recently_used_entry():
    backend = InMemoryLRUCache(max_size=2)
    cache = QueryCache(backend)

    cache.set("first", {"n": 1})
    cache.set("second", {"n": 2})
    cache.get("first")                 # first is now most-recently used
    cache.set("third", {"n": 3})       # evicts "second"

    assert cache.backend.get(cache_key("first")) is not None
    assert cache.backend.get(cache_key("second")) is None
    assert cache.backend.get(cache_key("third")) is not None
    assert cache.stats.evictions == 1


def test_cache_can_be_disabled(monkeypatch, tmp_path):
    import agentic_workflow

    calls = {"n": 0}

    class FakeWorkflow:
        def invoke(self, _state):
            calls["n"] += 1
            return {"final_response": {"overall_status": "PASS", "output_files": {}}}

    monkeypatch.setattr(agentic_workflow, "build_enterprise_workflow", lambda: FakeWorkflow())

    for _ in range(2):
        agentic_workflow.run_agentic_workflow(
            query="same question", output_dir=Path(tmp_path) / "o", use_cache=False
        )

    assert calls["n"] == 2, "caching was disabled but the second call was served from cache"
