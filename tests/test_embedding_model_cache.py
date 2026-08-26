"""The embedding model is constructed once per process (M5 Task 1, AUDIT.md F-09).

F-09: `SentenceTransformer(...)` was called inside `run_hybrid_retrieval`, which runs
per request. Measured cost was ~1.15s per query against a ~10ms encode.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.requires_stack


@pytest.fixture
def retriever(project_root):
    from dotenv import load_dotenv

    load_dotenv(project_root / ".env")

    import hybrid_retriever

    return hybrid_retriever


def test_repeated_calls_return_the_same_instance(retriever):
    settings = retriever.load_settings()

    first = retriever.get_embedding_model(settings)
    second = retriever.get_embedding_model(settings)

    assert first is second, "a second call rebuilt the model instead of reusing it"


def test_an_injected_model_is_used_verbatim(retriever):
    """The API hands down the instance it warmed at startup; tests inject stubs."""
    sentinel = object()

    assert retriever.get_embedding_model(model=sentinel) is sentinel


def test_the_cache_reports_whether_it_is_warm(retriever):
    settings = retriever.load_settings()

    retriever._EMBEDDING_MODELS.clear()
    assert retriever.embedding_model_is_loaded(settings) is False

    retriever.get_embedding_model(settings)
    assert retriever.embedding_model_is_loaded(settings) is True


def test_construction_is_not_repeated_under_concurrent_first_use(retriever, monkeypatch):
    """Two threads racing the first call must not build two models."""
    import threading

    retriever._EMBEDDING_MODELS.clear()

    built = {"n": 0}
    real = retriever.SentenceTransformer

    def counting(*args, **kwargs):
        built["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(retriever, "SentenceTransformer", counting)

    settings = retriever.load_settings()
    results = []

    def worker():
        results.append(retriever.get_embedding_model(settings))

    threads = [threading.Thread(target=worker) for _ in range(4)]

    for t in threads:
        t.start()

    for t in threads:
        t.join()

    assert built["n"] == 1, f"model was constructed {built['n']} times under a race"
    assert len({id(r) for r in results}) == 1


def test_no_module_constructs_the_model_inside_a_request_path():
    """Guards the regression: the load must not drift back into per-request code."""
    import ast
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "src" / "retrieval" / "hybrid_retriever.py"
    ).read_text(encoding="utf-8")

    tree = ast.parse(source)

    offenders = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue

        if node.name in {"get_embedding_model"}:
            continue

        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "SentenceTransformer"
            ):
                offenders.append(node.name)

    assert not offenders, (
        f"SentenceTransformer is constructed inside {offenders}; it must only be built "
        "by get_embedding_model, which caches it per process (F-09)"
    )
