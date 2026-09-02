"""API authentication, error sanitisation and a real /health (M6 Tasks 4 and 5)."""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def api(project_root):
    load_dotenv(project_root / ".env")

    if not os.getenv("API_BEARER_TOKEN"):
        pytest.skip("API_BEARER_TOKEN is not configured")

    import main as api_main  # src/api is on sys.path via conftest

    return api_main


@pytest.fixture
def client(api):
    with TestClient(api.app) as test_client:
        yield test_client


def token() -> str:
    return os.environ["API_BEARER_TOKEN"]


# --------------------------------------------------------------------------------
# Task 4: authentication
# --------------------------------------------------------------------------------


def test_authenticated_request_is_accepted(client, api, monkeypatch):
    """An authenticated caller reaches the workflow.

    The workflow itself is stubbed: what is under test is the auth gate, and a real
    run would cost three LLM calls per assertion.
    """
    def fake_workflow(query, output_dir, **_kwargs):
        return {
            "overall_status": "PASS",
            "run_id": "stub_run",
            "query": query,
            "answer_provider": "gemini",
            "answer_model": "test",
            "answer_length_chars": 42,
            "source_summary": {},
            "evaluation_summary": {},
            "output_files": {},
            "answer_preview": "stubbed",
        }

    monkeypatch.setattr(api, "run_agentic_workflow", fake_workflow)

    response = client.post(
        "/query",
        json={"query": "which sellers are least reliable"},
        headers={"Authorization": f"Bearer {token()}"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["overall_status"] == "PASS"


@pytest.mark.parametrize(
    "headers",
    [
        pytest.param({}, id="no-header"),
        pytest.param({"Authorization": "Bearer wrong-token"}, id="wrong-token"),
        pytest.param({"Authorization": "Bearer "}, id="empty-token"),
        pytest.param({"Authorization": "Basic abc123"}, id="wrong-scheme"),
    ],
)
def test_unauthenticated_request_returns_401_with_no_internal_detail(client, headers):
    """401, never 403, never 500 -- and nothing about the internals in the body."""
    response = client.post(
        "/query", json={"query": "which sellers are least reliable"}, headers=headers
    )

    assert response.status_code == 401, (
        f"expected 401, got {response.status_code}: {response.text}"
    )

    body = response.text.lower()

    for leak in (
        "localhost", "127.0.0.1", "5432", "7687", "6333",
        "password", "postgresql://", "bolt://", "traceback",
        "enterprise_readonly", "api_key",
    ):
        assert leak not in body, f"unauthenticated 401 leaked {leak!r}: {response.text}"


def test_a_failing_workflow_does_not_leak_connection_detail(client, api, monkeypatch):
    """F-08: the handler returned the psycopg2 error, host and port included."""
    def explode(*_args, **_kwargs):
        raise RuntimeError(
            'connection to server at "localhost" (::1), port 5432 failed: '
            "password authentication failed for user \"enterprise_user\""
        )

    monkeypatch.setattr(api, "run_agentic_workflow", explode)

    response = client.post(
        "/query",
        json={"query": "which sellers are least reliable"},
        headers={"Authorization": f"Bearer {token()}"},
    )

    assert response.status_code == 500
    body = response.text.lower()

    for leak in ("localhost", "5432", "password", "enterprise_user", "traceback"):
        assert leak not in body, f"500 response leaked {leak!r}: {response.text}"

    assert "incident_id" in response.text, "an opaque error needs a reference to trace"


def test_cache_stats_is_readable_without_a_token(client):
    """Operational counters carry no data; requiring a token would just hide them."""
    assert client.get("/cache/stats").status_code == 200


# --------------------------------------------------------------------------------
# Task 5: /health actually checks dependencies
# --------------------------------------------------------------------------------


def test_health_returns_503_and_names_the_failed_dependency(client, api, monkeypatch):
    """F-07: /health reported ok while every dependency was unreachable."""
    def dead(*_args, **_kwargs):
        raise ConnectionError("connection refused (simulated)")

    monkeypatch.setitem(api.DEPENDENCY_CHECKS, "neo4j", dead)

    response = client.get("/health")

    assert response.status_code == 503, (
        f"a dead dependency must not report healthy: {response.status_code}"
    )

    payload = response.json()

    assert payload["status"] == "degraded"
    assert "neo4j" in payload["failed"]
    assert payload["dependencies"]["neo4j"]["status"] == "down"
    assert "ConnectionError" in payload["dependencies"]["neo4j"]["error"]


def test_health_reports_every_dependency_down_when_all_fail(client, api, monkeypatch):
    def dead(*_args, **_kwargs):
        raise ConnectionError("connection refused (simulated)")

    for name in list(api.DEPENDENCY_CHECKS):
        monkeypatch.setitem(api.DEPENDENCY_CHECKS, name, dead)

    response = client.get("/health")

    assert response.status_code == 503
    assert set(response.json()["failed"]) == {"postgres", "neo4j", "qdrant"}


def test_health_states_that_gemini_is_not_probed(client):
    """An unchecked dependency must be named as unchecked, not implied healthy."""
    payload = client.get("/health").json()

    assert "gemini" in payload["not_checked"]


@pytest.mark.requires_stack
def test_health_returns_200_when_the_stack_is_up(client):
    response = client.get("/health")

    assert response.status_code == 200, response.text

    payload = response.json()

    assert payload["status"] == "ok"
    assert payload["failed"] == []
    assert set(payload["dependencies"]) == {"postgres", "neo4j", "qdrant"}

    for name, result in payload["dependencies"].items():
        assert result["status"] == "ok", f"{name}: {result}"
        assert result["latency_ms"] >= 0
