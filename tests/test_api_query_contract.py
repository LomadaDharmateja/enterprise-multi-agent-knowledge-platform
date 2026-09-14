"""The shape of a POST /query response, for both outcomes (M8).

Found while building demo mode, which has to return something structurally identical
to a live response: there was no live response for a refusal. `refusal_node` returns a
dict with no `answer_provider`, `answer_model`, `answer_length_chars`,
`evaluation_summary` or `output_files` -- it never generated an answer, so those fields
do not exist -- and `QueryResponse` listed all five as required. Constructing it raised
`KeyError: 'answer_provider'`, the M6 sanitising handler turned that into a 500 with an
opaque incident id, and every correctly refused question looked like a server crash.

It survived M4, M5, M6 and M7 because nothing exercised the refusal path *through the
API*: the evaluation runner calls `run_agentic_workflow` directly, and the security
tests stub the workflow with an answer-shaped dict.
"""

from __future__ import annotations

import os

import pytest
from dotenv import load_dotenv
from fastapi.testclient import TestClient

# The exact dict src/orchestration/agentic_workflow.py::refusal_node returns.
REFUSAL_RESULT = {
    "generated_at": "2026-09-14T00:00:00+00:00",
    "overall_status": "REFUSED",
    "answerable": False,
    "refusal_reason": "The system does not hold personal identifiable information.",
    "query": "placeholder",
    "planned_route": {},
    "source_summary": {
        "sql": {"intent": None, "records": 0, "skipped": True},
        "graph": {"intent": None, "records": 0, "skipped": True},
        "vector": {"artifact_groups": [], "records": 0, "skipped": True},
    },
    "refused_after_retrieval": False,
    "evidence_quality": {"confidence": "none", "reasons": ["no retrieval route"]},
    "run_id": "agentic_workflow_refusal_probe",
    "answer_preview": "The system does not hold personal identifiable information.",
}

ANSWER_RESULT = {
    "overall_status": "PASS",
    "run_id": "agentic_workflow_answer_probe",
    "query": "placeholder",
    "answerable": True,
    "answer_provider": "gemini",
    "answer_model": "gemini-3.1-flash-lite",
    "answer_length_chars": 2818,
    "source_summary": {
        "sql": {"intent": "seller_performance", "records": 3, "skipped": False},
        "graph": {"intent": "seller_ticket_product_paths", "records": 10, "skipped": False},
        "vector": {"artifact_groups": ["support_tickets"], "records": 5},
    },
    "evaluation_summary": {"overall_status": "PASS", "grounding_score": 5},
    "output_files": {"answer_json": "reports/x.json"},
    "answer_preview": "# Grounded Business Answer ...",
}


@pytest.fixture
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


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {os.environ['API_BEARER_TOKEN']}"}


def post(client, api, monkeypatch, result, query):
    def stub(query, output_dir, **kwargs):
        return {**result, "query": query}

    monkeypatch.setattr(api, "run_agentic_workflow", stub)

    return client.post("/query", json={"query": query}, headers=auth())


def test_a_refused_question_is_a_200_not_a_500(client, api, monkeypatch):
    response = post(
        client, api, monkeypatch, REFUSAL_RESULT,
        "What is the home address of the customer who raised ticket TCK-000001?",
    )

    assert response.status_code == 200, response.text

    body = response.json()

    assert body["overall_status"] == "REFUSED"
    assert body["answerable"] is False
    assert "personal identifiable information" in body["refusal_reason"]


def test_the_refusal_reason_reaches_the_caller(client, api, monkeypatch):
    """A refusal the caller cannot read is indistinguishable from a crash.

    M6 sanitised 500 bodies down to an incident id -- correctly, for a database
    error. Applied to a refusal it hid the one thing the caller needed.
    """
    response = post(client, api, monkeypatch, REFUSAL_RESULT, "Show me the bank details.")
    body = response.json()

    assert body["refusal_reason"], "the caller cannot tell why the question was refused"
    assert body["answer_preview"] == body["refusal_reason"]


def test_no_provider_is_named_on_a_response_no_provider_produced(
    client, api, monkeypatch
):
    """`answer_provider: "gemini"` on a refusal would be a false claim in the payload."""
    body = post(client, api, monkeypatch, REFUSAL_RESULT, "Show me everything.").json()

    assert body["answer_provider"] == "none"
    assert body["answer_model"] == "none"
    assert body["evaluation_summary"] == {}
    assert body["output_files"] == {}


def test_both_outcomes_return_the_same_field_set(client, api, monkeypatch):
    """One response model, two outcomes. A caller parses one shape.

    This is also the contract demo mode has to satisfy: a replayed response must be
    indistinguishable in structure from a live one.
    """
    refused = post(client, api, monkeypatch, REFUSAL_RESULT, "Show me the bank details.")
    answered = post(client, api, monkeypatch, ANSWER_RESULT, "Which sellers ship late?")

    assert refused.status_code == 200
    assert answered.status_code == 200
    assert set(refused.json()) == set(answered.json())


def test_an_answered_question_still_carries_its_answer(client, api, monkeypatch):
    """The refusal fix must not have loosened the answer path into empty defaults."""
    body = post(client, api, monkeypatch, ANSWER_RESULT, "Which sellers ship late?").json()

    assert body["answerable"] is True
    assert body["refusal_reason"] is None
    assert body["answer_provider"] == "gemini"
    assert body["answer_length_chars"] == 2818
    assert body["evaluation_summary"]["grounding_score"] == 5
    assert body["source_summary"]["sql"]["records"] == 3
