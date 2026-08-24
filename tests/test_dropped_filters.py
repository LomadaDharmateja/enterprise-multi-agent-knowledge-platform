"""Dropped filters must be visible, and a fully-dropped filter set must be flagged.

Two failure modes, both requested at review:

1. A question naming an entity that does not exist must run without that filter, and
   the dropped value must be recorded rather than vanishing.
2. When EVERY filter drops, the query silently degrades into the unfiltered one that
   F-03 was about -- and an unfiltered table looks exactly like a filtered one. The
   response must carry a low-confidence signal instead of a confident answer.

The confidence signal is asserted on the plan and the context, not on the model's
prose, because prose is not deterministic and a test that depends on it cannot fail
reliably.
"""

from __future__ import annotations

import pytest

from gemini_query_planner import resolve_plan_parameters, validate_and_normalize_plan
from hybrid_retriever import assess_evidence_quality

VOCABULARY = {
    "category_by_any_name": {
        "moveis_decoracao": "moveis_decoracao",
        "furniture_decor": "moveis_decoracao",
    },
    "english_by_category_id": {"moveis_decoracao": "furniture_decor"},
    "seller_states": ["SP", "RJ"],
    "customer_states": ["SP", "RJ"],
    "order_statuses": ["delivered", "canceled"],
    "severities": ["critical", "high", "medium", "low"],
    "issue_types": ["product_quality_complaint"],
    "incident_types": ["carrier_backlog"],
    "claim_statuses": ["approved"],
    "policy_topics": ["late_delivery_compensation"],
    "sentiment_labels": ["negative"],
    "delivery_statuses": ["delivered", "not_delivered"],
}


def plan_for(raw: dict) -> dict:
    """Run a raw planner response through the real validation and resolution path."""
    normalized = validate_and_normalize_plan(raw)
    return resolve_plan_parameters(normalized, raw, VOCABULARY)


# --------------------------------------------------------------------------------
# 1. An unresolvable entity name is recorded, and the query still runs
# --------------------------------------------------------------------------------


def test_unresolvable_entity_is_dropped_recorded_and_the_query_still_runs():
    """"Which sellers in Wakanda sell artisanal moon cheese?" -- neither exists."""
    plan = plan_for(
        {
            "answerable": True,
            "sql_intent": "product_performance",
            "graph_intent": None,
            "vector_artifact_groups": ["support_tickets"],
            "sql_filters": {
                "product_categories": ["artisanal moon cheese"],
                "min_orders": 10,
            },
            "sql_sort_by": "total_items_sold",
            "graph_filters": {},
            "reasoning": "product question",
        }
    )

    dropped_keys = {d["key"] for d in plan["dropped_filters"]}
    assert "product_categories" in dropped_keys

    entry = next(
        d for d in plan["dropped_filters"] if d["key"] == "product_categories"
    )
    assert entry["value"] == "artisanal moon cheese"
    assert entry["reason"] == "not in category vocabulary"

    # The query still runs: the resolvable filter and the sort survive.
    assert plan["sql_plan"]["filters"] == {"min_orders": 10}
    assert plan["sql_plan"]["sort_by"] == "total_items_sold"
    assert "product_categories" not in plan["sql_plan"]["filters"]


def test_a_partially_dropped_plan_is_medium_confidence_not_low():
    plan = plan_for(
        {
            "answerable": True,
            "sql_intent": "seller_performance",
            "graph_intent": None,
            "vector_artifact_groups": ["support_tickets"],
            "sql_filters": {"seller_states": ["SP"], "min_orders": "many"},
            "graph_filters": {},
            "reasoning": "seller question",
        }
    )

    quality = assess_evidence_quality(
        proposed_filter_count=plan["filters_proposed"],
        accepted_filter_count=plan["filters_accepted"],
        dropped_filters=plan["dropped_filters"],
        leg_results=[
            {"source": "postgresql", "record_count": 10, "skipped": False},
            {"source": "neo4j", "record_count": 0, "skipped": True},
            {"source": "qdrant", "record_count": 5, "skipped": False},
        ],
    )

    assert quality["confidence"] == "medium"
    assert quality["all_filters_dropped"] is False


# --------------------------------------------------------------------------------
# 2. Every filter drops -> low confidence, not a confident unfiltered answer
# --------------------------------------------------------------------------------


def test_when_every_filter_drops_the_response_is_flagged_low_confidence():
    plan = plan_for(
        {
            "answerable": True,
            "sql_intent": "seller_performance",
            "graph_intent": "seller_ticket_product_paths",
            "vector_artifact_groups": ["support_tickets"],
            "sql_filters": {
                "seller_ids": ["the biggest seller in Wakanda"],
                "seller_states": ["Atlantis"],
            },
            "graph_filters": {"severities": ["apocalyptic"]},
            "reasoning": "seller question naming nothing that exists",
        }
    )

    assert plan["filters_accepted"] == 0
    assert plan["filters_proposed"] == 3
    assert len(plan["dropped_filters"]) == 3
    assert plan["sql_plan"]["filters"] == {}
    assert plan["graph_plan"]["filters"] == {}

    quality = assess_evidence_quality(
        proposed_filter_count=plan["filters_proposed"],
        accepted_filter_count=plan["filters_accepted"],
        dropped_filters=plan["dropped_filters"],
        leg_results=[
            {"source": "postgresql", "record_count": 10, "skipped": False},
            {"source": "neo4j", "record_count": 10, "skipped": False},
            {"source": "qdrant", "record_count": 5, "skipped": False},
        ],
    )

    assert quality["confidence"] == "low", (
        "every filter dropped, so the queries ran unfiltered -- the response must not "
        "present this as evidence selected for the question"
    )
    assert quality["all_filters_dropped"] is True
    assert quality["reasons"]
    assert "failed to resolve" in quality["reasons"][0]

    # All three dropped values must be nameable in the signal, not just counted.
    joined = " ".join(quality["reasons"])
    for value in ("Wakanda", "Atlantis", "apocalyptic"):
        assert value in joined, f"{value} is not visible in the confidence signal"


def test_a_fully_resolved_plan_is_high_confidence():
    plan = plan_for(
        {
            "answerable": True,
            "sql_intent": "seller_performance",
            "graph_intent": None,
            "vector_artifact_groups": ["support_tickets"],
            "sql_filters": {"seller_states": ["SP"], "min_orders": 5},
            "sql_sort_by": "late_delivery_rate",
            "graph_filters": {},
            "reasoning": "seller question",
        }
    )

    assert plan["dropped_filters"] == []

    quality = assess_evidence_quality(
        proposed_filter_count=plan["filters_proposed"],
        accepted_filter_count=plan["filters_accepted"],
        dropped_filters=plan["dropped_filters"],
        leg_results=[
            {"source": "postgresql", "record_count": 10, "skipped": False},
            {"source": "qdrant", "record_count": 5, "skipped": False},
        ],
    )

    assert quality["confidence"] == "high"


def test_no_filters_proposed_is_not_treated_as_all_filters_dropped():
    """A question that legitimately needs no filter must not be flagged low."""
    quality = assess_evidence_quality(
        proposed_filter_count=0,
        accepted_filter_count=0,
        dropped_filters=[],
        leg_results=[
            {"source": "postgresql", "record_count": 10, "skipped": False},
            {"source": "qdrant", "record_count": 5, "skipped": False},
        ],
    )

    assert quality["confidence"] == "high"
    assert quality["all_filters_dropped"] is False


# --------------------------------------------------------------------------------
# The signal has to reach the answer prompt, not just the report
# --------------------------------------------------------------------------------


def test_low_confidence_reaches_the_answer_prompt_text():
    from retrieval_context_builder import build_context_text

    context = {
        "business_question": "Which sellers in Wakanda are worst?",
        "source_summary": {
            "sql": {
                "intent": "seller_performance",
                "records": 10,
                "skipped": False,
                "filters": {},
                "sort_by": "late_delivery_rate",
            },
            "graph": {
                "intent": None,
                "records": 0,
                "skipped": True,
                "filters": {},
            },
            "vector": {"artifact_groups": ["support_tickets"], "records": 5},
        },
        "evidence_quality": {
            "confidence": "low",
            "all_filters_dropped": True,
            "reasons": ["all 1 filter value(s) proposed for this question failed"],
        },
        "recommended_next_actions": ["review evidence"],
        "entity_ids": {},
    }

    text = build_context_text(context)

    assert "Evidence confidence: low" in text
    assert "TREAT THIS EVIDENCE AS UNFILTERED" in text
    assert "not selected by the plan" in text, "a skipped leg must be stated as skipped"
