"""The UI shows the evidence behind every answer (M8 Task 4).

"The evidence trail *is* the product" is the rebuild plan's line for this milestone, so
it gets asserted rather than eyeballed. Streamlit's AppTest runs the real script
headlessly and exposes what it rendered, which is a stronger check than a screenshot:
a screenshot proves the page looked right once, these fail if a field stops being shown.

The API is stubbed at the `requests` boundary with a real recorded response from
demo/scenarios.json, so these need no server, no database and no API key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP = PROJECT_ROOT / "src" / "ui" / "streamlit_app.py"
BUNDLE = PROJECT_ROOT / "demo" / "scenarios.json"


@pytest.fixture(scope="module")
def bundle():
    if not BUNDLE.exists():
        pytest.skip("demo/scenarios.json has not been built")

    return json.loads(BUNDLE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def answered(bundle):
    return next(s for s in bundle["scenarios"] if s["id"] == "C33")


@pytest.fixture(scope="module")
def refused(bundle):
    return next(s for s in bundle["scenarios"] if s["id"] == "D51")


def run_app(monkeypatch, scenario, bundle, click_run: bool = True) -> AppTest:
    """Drive the real app with a recorded response stubbed in at the HTTP boundary."""
    import requests

    health = {
        "status": "ok", "mode": "demo", "dependencies": {}, "failed": [],
        "demo": {"scenario_count": bundle["scenario_count"]},
    }

    scenarios_payload = {
        "mode": "demo",
        "bundle": {"scenario_count": bundle["scenario_count"]},
        "scenarios": [
            {
                "id": s["id"], "question": s["question"], "category": s["category"],
                "headline": s["headline"], "shows": s["shows"],
                "caveat": s.get("caveat"), "run_id": s["response"]["run_id"],
                "outcome": s["response"]["overall_status"],
                "answerable": s["response"]["answerable"],
                "route": s["route"], "evidence": s["evidence"],
                "metrics": s["metrics"],
                "grounding_score": (s.get("evaluator") or {}).get("grounding_score"),
                "route_reproduces_today": s.get("route_reproduces_today", {}),
            }
            for s in bundle["scenarios"]
        ],
    }

    trace = {
        "run_id": scenario["response"]["run_id"],
        "span_count": len(scenario["trace"]["spans"]),
        "spans": scenario["trace"]["spans"],
        "reconstructed": True,
    }

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload
            self.status_code = 200

        def json(self):
            return self._payload

        def raise_for_status(self):
            return None

    def fake_get(url, **kwargs):
        if url.endswith("/health"):
            return FakeResponse(health)
        if url.endswith("/demo/scenarios"):
            return FakeResponse(scenarios_payload)
        if "/traces/" in url:
            return FakeResponse(trace)
        raise AssertionError(f"unexpected GET {url}")

    def fake_post(url, **kwargs):
        assert url.endswith("/query")
        return FakeResponse(scenario["response"])

    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(requests, "post", fake_post)

    app = AppTest.from_file(str(APP), default_timeout=60)
    app.run()

    if click_run:
        app.text_area[0].set_value(scenario["question"])

        # By label, not by index: AppTest lists main-body elements before sidebar
        # ones, so positional selection silently clicks "Use this question" instead
        # and every assertion then fails against a page that never ran the query.
        next(b for b in app.button if b.label == "Run").click().run()

    return app


def page_text(app: AppTest) -> str:
    parts: list[str] = []

    for collection in ("markdown", "caption", "success", "info", "warning", "error"):
        parts += [str(element.value) for element in getattr(app, collection, [])]

    return "\n".join(parts)


def metric_labels(app: AppTest) -> dict[str, str]:
    return {m.label: str(m.value) for m in app.metric}


# --------------------------------------------------------------------------------
# The six things Task 4 requires
# --------------------------------------------------------------------------------


def test_the_answer_text_is_shown(monkeypatch, answered, bundle):
    app = run_app(monkeypatch, answered, bundle)

    assert "Grounded Business Answer" in page_text(app)


def test_the_route_is_shown(monkeypatch, answered, bundle):
    """sql_intent, graph_intent and the vector artifact groups."""
    app = run_app(monkeypatch, answered, bundle)
    text = page_text(app)

    summary = answered["response"]["source_summary"]

    assert summary["sql"]["intent"] in text
    assert summary["graph"]["intent"] in text

    for group in summary["vector"]["artifact_groups"]:
        assert group in text


def test_the_per_leg_record_counts_are_shown(monkeypatch, answered, bundle):
    app = run_app(monkeypatch, answered, bundle)

    counts = [
        m.value for m in app.metric if m.label == "records returned"
    ]

    assert len(counts) == 3, "expected one record count per store"

    summary = answered["response"]["source_summary"]
    expected = sorted([
        summary["sql"]["records"],
        summary["graph"]["records"],
        summary["vector"]["records"],
    ])

    assert sorted(int(c) for c in counts) == expected


def test_cost_and_latency_are_shown(monkeypatch, answered, bundle):
    app = run_app(monkeypatch, answered, bundle)
    metrics = metric_labels(app)

    expected_cost = answered["response"]["metrics"]["cost_usd"]

    assert "Cost" in metrics
    assert f"{expected_cost:.6f}" in metrics["Cost"]
    assert "Latency" in metrics
    assert metrics["Latency"] != "—"


def test_the_trace_link_is_shown(monkeypatch, answered, bundle):
    app = run_app(monkeypatch, answered, bundle)
    text = page_text(app)

    run_id = answered["response"]["run_id"]

    assert run_id in text
    assert f"/traces/{run_id}" in text


def test_the_span_breakdown_is_rendered(monkeypatch, answered, bundle):
    """Which stage the time went to, not just a total."""
    app = run_app(monkeypatch, answered, bundle)
    text = page_text(app)

    for span in answered["trace"]["spans"]:
        assert span["name"] in text


# --------------------------------------------------------------------------------
# Honesty in the presentation
# --------------------------------------------------------------------------------


def test_a_refusal_shows_the_reason_and_no_answer(monkeypatch, refused, bundle):
    app = run_app(monkeypatch, refused, bundle)
    text = page_text(app)

    assert "personal identifiable information" in text
    assert "No answer was generated" in text


def test_the_evaluator_scores_carry_their_own_caveat(monkeypatch, answered, bundle):
    """The judge scored 0.390 kappa against a human. Showing 5/5 without that is a
    stronger claim than the measurement supports."""
    app = run_app(monkeypatch, answered, bundle)
    text = page_text(app)

    assert "0.390" in text
    assert "kappa" in text.lower()


def test_the_page_leads_with_evidence_not_with_the_answer(monkeypatch, answered, bundle):
    """Ordering is the claim this milestone makes. If the answer moves above the
    evidence, the page is asserting the answer and offering the evidence as an
    optional extra."""
    headings = [m.value for m in app_markdowns(run_app(monkeypatch, answered, bundle))]

    evidence = next(i for i, h in enumerate(headings) if "Evidence trail" in h)
    answer = next(i for i, h in enumerate(headings) if h.strip() == "### Answer")

    assert evidence < answer


def app_markdowns(app: AppTest):
    return list(app.markdown)


# --------------------------------------------------------------------------------
# Deployment plumbing
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Render's blueprint injects a service address with no scheme.
        ("enterprise-ai-demo-api.onrender.com", "https://enterprise-ai-demo-api.onrender.com"),
        ("https://x.onrender.com/", "https://x.onrender.com"),
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("localhost:8000", "http://localhost:8000"),
        ("", ""),
    ],
)
def test_a_scheme_less_api_host_is_usable(raw, expected):
    """`requests` rejects a bare hostname, and the failure reads as "the API is down".

    Found while writing render.yaml: `fromService: property: host` yields
    `name.onrender.com`, not a URL.
    """
    import sys

    sys.path.insert(0, str(PROJECT_ROOT / "src" / "ui"))

    import streamlit_app

    assert streamlit_app.normalise_base_url(raw) == expected
