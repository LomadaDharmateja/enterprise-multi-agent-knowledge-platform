"""Live boundary tests for F-03: does the question change the evidence?

The audit's finding was that it did not -- a business question and "purple monkey
dishwasher" produced byte-identical SQL and graph evidence, because no template
accepted a parameter other than the row limit. These tests hold the fix in place by
running real queries against the live stack and comparing evidence hashes.
"""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from dotenv import load_dotenv

pytestmark = pytest.mark.requires_stack


def evidence_hash(records: list[dict]) -> str:
    """Order-sensitive digest of a result set. Ordering is part of the evidence."""
    return hashlib.sha256(
        json.dumps(records, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]


@pytest.fixture(scope="module")
def engines(project_root):
    load_dotenv(project_root / ".env")

    from hybrid_retriever import build_neo4j_driver, build_postgres_engine, load_settings

    settings = load_settings()

    try:
        engine = build_postgres_engine(settings)
        driver = build_neo4j_driver(settings)

        with engine.connect():
            pass

        driver.verify_connectivity()
    except Exception as exc:  # noqa: BLE001 -- any failure means "no stack"
        pytest.skip(f"stack not reachable: {exc}")

    yield engine, driver

    engine.dispose()
    driver.close()


# --------------------------------------------------------------------------------
# The Task 3 boundary test
# --------------------------------------------------------------------------------


def test_same_template_different_filters_returns_different_evidence(engines):
    """Two questions, one template, different bound values -> different evidence.

    These are the two bound examples from docs/M2_RETRIEVAL_DESIGN.md 1.1: the
    flagship complaint question and "Which seller had the highest revenue?". Both
    route to seller_performance. Under F-03 they were byte-identical.
    """
    engine, _ = engines

    from hybrid_retriever import sql_retrieve

    complaints = sql_retrieve(
        engine=engine,
        query="Find sellers with negative customer complaints",
        limit=10,
        forced_intent="seller_performance",
        filters={"min_orders": 5, "max_avg_review_score": 4.0},
        sort_by="late_delivery_rate",
    )

    revenue = sql_retrieve(
        engine=engine,
        query="Which seller had the highest revenue?",
        limit=10,
        forced_intent="seller_performance",
        filters={"min_orders": 1},
        sort_by="total_item_revenue",
    )

    assert complaints["record_count"] > 0
    assert revenue["record_count"] > 0

    assert evidence_hash(complaints["records"]) != evidence_hash(revenue["records"]), (
        "the same template returned identical evidence for two different questions "
        "-- this is F-03"
    )

    complaint_sellers = {r["seller_id"] for r in complaints["records"]}
    revenue_sellers = {r["seller_id"] for r in revenue["records"]}

    assert not (complaint_sellers & revenue_sellers), (
        "the complaint question and the revenue question named the same sellers"
    )

    # The substantive fix, not just a different hash: the complaint question must not
    # be answered with the largest sellers. Under the old ORDER BY it always was.
    assert max(r["total_orders"] for r in complaints["records"]) < min(
        r["total_orders"] for r in revenue["records"]
    )


def test_a_filter_actually_narrows_the_result(engines):
    engine, _ = engines

    from hybrid_retriever import sql_retrieve

    unfiltered = sql_retrieve(
        engine=engine, query="x", limit=10, forced_intent="order_summary", filters={}
    )
    filtered = sql_retrieve(
        engine=engine,
        query="x",
        limit=10,
        forced_intent="order_summary",
        filters={"order_statuses": ["canceled"]},
    )

    assert evidence_hash(unfiltered["records"]) != evidence_hash(filtered["records"])
    assert {r["order_status"] for r in filtered["records"]} == {"canceled"}


def test_state_filter_partitions_the_seller_evidence(engines):
    engine, _ = engines

    from hybrid_retriever import sql_retrieve

    sp = sql_retrieve(
        engine=engine,
        query="x",
        limit=10,
        forced_intent="seller_performance",
        filters={"seller_states": ["SP"]},
    )
    rj = sql_retrieve(
        engine=engine,
        query="x",
        limit=10,
        forced_intent="seller_performance",
        filters={"seller_states": ["RJ"]},
    )

    assert {r["seller_state"] for r in sp["records"]} == {"SP"}
    assert {r["seller_state"] for r in rj["records"]} == {"RJ"}
    assert evidence_hash(sp["records"]) != evidence_hash(rj["records"])


# --------------------------------------------------------------------------------
# Graph ordering and filtering
# --------------------------------------------------------------------------------


def test_severity_orders_by_rank_not_alphabetically(engines):
    """`ORDER BY t.severity DESC` sorted a string: medium > low > high > critical."""
    _, driver = engines

    from hybrid_retriever import graph_retrieve

    result = graph_retrieve(
        driver=driver,
        query="x",
        limit=10,
        forced_intent="seller_ticket_product_paths",
        filters={},
    )

    severities = [r["severity"] for r in result["records"]]

    assert severities, "graph leg returned nothing"
    assert severities[0] == "critical", f"top severity is {severities[0]}, not critical"

    rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    ranks = [rank[s] for s in severities]
    assert ranks == sorted(ranks, reverse=True), severities


def test_graph_category_evidence_is_not_null(engines):
    """D-2: Neo4j Category nodes had no product_category_name_english at all.

    Three templates return that property, so every graph result reached the LLM with
    `category: null` until the Category load was re-run.
    """
    _, driver = engines

    from hybrid_retriever import graph_retrieve

    result = graph_retrieve(
        driver=driver,
        query="x",
        limit=10,
        forced_intent="seller_ticket_product_paths",
        filters={},
    )

    categories = [r["category"] for r in result["records"]]

    assert categories
    assert all(c for c in categories), f"null categories in graph evidence: {categories}"


def test_graph_filters_narrow_the_traversal(engines):
    _, driver = engines

    from hybrid_retriever import graph_retrieve

    unfiltered = graph_retrieve(
        driver=driver, query="x", limit=10,
        forced_intent="seller_ticket_product_paths", filters={},
    )
    filtered = graph_retrieve(
        driver=driver, query="x", limit=10,
        forced_intent="seller_ticket_product_paths",
        filters={"category_ids": ["cama_mesa_banho"], "severities": ["critical"]},
    )

    assert evidence_hash(unfiltered["records"]) != evidence_hash(filtered["records"])
    assert {r["category_id"] for r in filtered["records"]} == {"cama_mesa_banho"}
    assert {r["severity"] for r in filtered["records"]} == {"critical"}


def test_policy_seller_filter_narrows_without_emptying(engines):
    """The seller filter on the policy template must narrow, not wipe out.

    The predicate is written to keep `all_sellers` policies, which store a selection
    rule instead of APPLIES_TO_SELLER edges. Measured limitation, recorded rather
    than papered over: only the 24 `category_scoped` policies are reachable through
    this template at all, because its base pattern requires an APPLIES_TO_CATEGORY
    edge and the 8 `all_sellers` + 8 `high_volume_sellers` policies have none. So the
    keep-all_sellers branch is correct but currently unreachable here; those policies
    reach the answer through the vector leg's `policy_seller_ids` instead.
    """
    _, driver = engines

    from hybrid_retriever import graph_retrieve

    unfiltered = graph_retrieve(
        driver=driver, query="x", limit=50,
        forced_intent="category_policy_guide_paths", filters={},
    )
    filtered = graph_retrieve(
        driver=driver, query="x", limit=50,
        forced_intent="category_policy_guide_paths",
        filters={"seller_ids": ["06a2c3af7b3aee5d69171b0e14f0ee87"]},
    )

    assert unfiltered["record_count"] > 0
    assert filtered["record_count"] > 0, "the seller filter emptied the policy leg"
    assert evidence_hash(unfiltered["records"]) != evidence_hash(filtered["records"])

    # Every policy returned under the filter must genuinely apply to that seller.
    assert {r["applicability_scope"] for r in filtered["records"]} <= {
        "category_scoped",
        "all_sellers",
    }


def test_policy_template_reaches_only_category_scoped_policies(engines):
    """Pins the limitation above so it cannot be forgotten or silently change."""
    _, driver = engines

    from hybrid_retriever import graph_retrieve

    result = graph_retrieve(
        driver=driver, query="x", limit=500,
        forced_intent="category_policy_guide_paths", filters={},
    )

    scopes = {r["applicability_scope"] for r in result["records"]}
    assert scopes == {"category_scoped"}, (
        f"policy template scope coverage changed: {scopes}. If all_sellers policies "
        "are now reachable, this limitation is fixed and the note in "
        "docs/M2_RETRIEVAL_DESIGN.md should be updated."
    )


# --------------------------------------------------------------------------------
# Evidence-linked filtering (D-1), both flag states
# --------------------------------------------------------------------------------


def test_evidence_linked_filtering_binds_ids_from_the_vector_leg():
    from hybrid_retriever import apply_evidence_links, harvest_entity_ids

    vector_results = {
        "results_by_artifact_group": {
            "support_tickets": [
                {"seller_id": "a" * 32, "order_id": "b" * 32},
                {"seller_id": "c" * 32, "order_id": "d" * 32},
                {"seller_id": "a" * 32, "order_id": "e" * 32},
            ]
        }
    }

    harvested = harvest_entity_ids(vector_results)
    assert harvested["seller_id"] == ["a" * 32, "c" * 32]

    merged, applied = apply_evidence_links(
        "sql", "seller_performance", {}, harvested
    )
    assert merged["seller_ids"] == ["a" * 32, "c" * 32]
    assert applied == ["seller_ids"]


def test_evidence_links_never_override_an_explicit_planner_filter():
    from hybrid_retriever import apply_evidence_links

    harvested = {"seller_id": ["a" * 32]}
    merged, applied = apply_evidence_links(
        "sql", "seller_performance", {"seller_ids": ["z" * 32]}, harvested
    )

    assert merged["seller_ids"] == ["z" * 32]
    assert applied == []


def test_flag_off_leaves_the_legs_independent(monkeypatch):
    """D-1 requires the independent-legs baseline to stay measurable."""
    from hybrid_retriever import evidence_linked_filtering_enabled

    monkeypatch.setenv("EVIDENCE_LINKED_FILTERING", "false")
    assert evidence_linked_filtering_enabled() is False

    monkeypatch.setenv("EVIDENCE_LINKED_FILTERING", "true")
    assert evidence_linked_filtering_enabled() is True

    # An explicit argument always wins over the environment.
    monkeypatch.setenv("EVIDENCE_LINKED_FILTERING", "true")
    assert evidence_linked_filtering_enabled(False) is False


def test_flag_off_reproduces_unlinked_evidence(engines):
    """With linking off, the SQL leg sees only what the planner supplied."""
    engine, _ = engines

    from hybrid_retriever import apply_evidence_links, sql_retrieve

    harvested = {"seller_id": ["06a2c3af7b3aee5d69171b0e14f0ee87"]}

    linked_filters, _ = apply_evidence_links(
        "sql", "seller_performance", {}, harvested
    )
    unlinked_filters, applied = apply_evidence_links(
        "sql", "seller_performance", {}, {}
    )

    assert applied == []

    linked = sql_retrieve(
        engine=engine, query="x", limit=10,
        forced_intent="seller_performance", filters=linked_filters,
    )
    unlinked = sql_retrieve(
        engine=engine, query="x", limit=10,
        forced_intent="seller_performance", filters=unlinked_filters,
    )

    assert evidence_hash(linked["records"]) != evidence_hash(unlinked["records"])
    assert linked["record_count"] == 1


# --------------------------------------------------------------------------------
# Skipped legs
# --------------------------------------------------------------------------------


def test_a_null_intent_skips_the_leg_rather_than_guessing():
    """Falling back to the keyword detector would turn the crash into a wrong answer."""
    from hybrid_retriever import skipped_leg_result

    result = skipped_leg_result("neo4j")

    assert result["skipped"] is True
    assert result["intent"] is None
    assert result["records"] == []
    assert result["record_count"] == 0
