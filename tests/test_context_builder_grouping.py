"""The context builder must group evidence by entity across all three legs.

AUDIT.md claim 52 was rated CAVEAT: "Group related evidence using entity IDs" collected
IDs from SQL and graph only, because the vector leg carried none (F-01). With the IDs
now on the Qdrant payload and the document text in the key the retriever reads, the
claim has to become true -- and "true" means a named seller_id is visible in the
assembled context alongside its SQL row and its graph path, not merely present in a
flat list of IDs.

Known records are fed straight through the builder, so nothing here depends on a live
stack or on what a particular query happens to retrieve.
"""

from __future__ import annotations

import pytest

from retrieval_context_builder import (
    build_context_text,
    build_document_evidence,
    build_entity_cross_references,
    build_graph_evidence,
    build_source_summary,
    build_sql_evidence,
    collect_entity_ids,
)

SELLER = "5145090ab595c0d0b8557199f5701fbf"
OTHER_SELLER = "06a2c3af7b3aee5d69171b0e14f0ee87"
ORDER = "b6b8305352b7f21764d7a8e573f1e8aa"
PRODUCT = "9969f7caa71f4ddb8127440e8d5890e8"
CUSTOMER = "cd2323501d2be6e4536cb19e12c6a92b"


def make_report() -> dict:
    """One seller present in all three legs; a second present in SQL only."""
    return {
        "query": "which sellers have complaints",
        "retrieval_results": {
            "sql": {
                "source": "postgresql",
                "intent": "seller_performance",
                "filters": {"min_orders": 5},
                "sort_by": "late_delivery_rate",
                "skipped": False,
                "record_count": 2,
                "records": [
                    {
                        "seller_id": SELLER,
                        "seller_state": "PR",
                        "total_orders": 12,
                        "avg_review_score": 2.4,
                        "late_delivery_orders": 5,
                        "late_delivery_rate": 0.4167,
                    },
                    {
                        "seller_id": OTHER_SELLER,
                        "seller_state": "MA",
                        "total_orders": 30,
                        "avg_review_score": 4.1,
                        "late_delivery_orders": 3,
                        "late_delivery_rate": 0.1,
                    },
                ],
            },
            "graph": {
                "source": "neo4j",
                "intent": "seller_ticket_product_paths",
                "filters": {"severities": ["critical"]},
                "skipped": False,
                "record_count": 1,
                "records": [
                    {
                        "seller_id": SELLER,
                        "seller_state": "PR",
                        "ticket_id": "TCK-002164",
                        "issue_type": "product_quality_complaint",
                        "severity": "critical",
                        "product_id": PRODUCT,
                        "category_id": "cama_mesa_banho",
                        "category": "bed_bath_table",
                    }
                ],
            },
            "vector": {
                "source": "qdrant",
                "artifact_groups": ["support_tickets", "policy_documents"],
                "record_count": 2,
                "results_by_artifact_group": {
                    "support_tickets": [
                        {
                            "score": 0.52,
                            "artifact_group": "support_tickets",
                            "artifact_type": "support_tickets",
                            "artifact_id": "support_tickets:TCK-002164",
                            "ticket_id": "TCK-002164",
                            "title": "Product Quality Complaint for bed_bath_table",
                            "severity": "critical",
                            "status": "closed",
                            "seller_id": SELLER,
                            "order_id": ORDER,
                            "product_id": PRODUCT,
                            "customer_id": CUSTOMER,
                            "text_preview": "Customer reports the item arrived damaged.",
                        }
                    ],
                    "policy_documents": [
                        {
                            "score": 0.44,
                            "artifact_group": "policy_documents",
                            "artifact_type": "policy_documents",
                            "artifact_id": "policy_documents:POL-000025",
                            "policy_id": "POL-000025",
                            "policy_topic": "damaged_goods_returns",
                            "title": "Damaged Goods Returns - Home And Furniture",
                            "text_preview": "Where an item arrives damaged, the seller must...",
                        }
                    ],
                },
            },
        },
        "summary": {
            "evidence_quality": {"confidence": "high", "reasons": []},
        },
    }


@pytest.fixture
def context():
    report = make_report()

    sql_evidence = build_sql_evidence(report["retrieval_results"]["sql"], max_records=5)
    graph_evidence = build_graph_evidence(
        report["retrieval_results"]["graph"], max_records=5
    )
    document_evidence = build_document_evidence(
        report["retrieval_results"]["vector"], max_records_per_group=5
    )

    built = {
        "business_question": report["query"],
        "source_summary": build_source_summary(
            sql_evidence=sql_evidence,
            graph_evidence=graph_evidence,
            document_evidence=document_evidence,
            sql_result=report["retrieval_results"]["sql"],
            graph_result=report["retrieval_results"]["graph"],
        ),
        "evidence_quality": report["summary"]["evidence_quality"],
        "sql_evidence": sql_evidence,
        "graph_evidence": graph_evidence,
        "document_evidence": document_evidence,
        "entity_ids": collect_entity_ids(report),
        "entity_cross_references": build_entity_cross_references(report),
        "recommended_next_actions": ["review the evidence"],
    }

    built["answer_context_text"] = build_context_text(built)

    return built


# --------------------------------------------------------------------------------
# Grouping across all three legs
# --------------------------------------------------------------------------------


def test_named_seller_is_grouped_across_all_three_legs(context):
    """The Task 4 assertion: one seller, three sources, visible as a group."""
    grouped = context["entity_cross_references"]["by_key"]["seller_id"]

    assert SELLER in grouped, f"{SELLER} is not grouped at all"

    entry = grouped[SELLER]

    assert entry["sources"] == ["graph", "sql", "vector"], entry["sources"]
    assert entry["source_count"] == 3

    joined = " ".join(entry["references"])
    assert "sql:" in joined
    assert "graph:" in joined
    assert "vector:" in joined


def test_a_single_source_seller_is_not_claimed_as_linked(context):
    """Grouping must discriminate, or it says nothing."""
    entry = context["entity_cross_references"]["by_key"]["seller_id"][OTHER_SELLER]

    assert entry["sources"] == ["sql"]
    assert entry["source_count"] == 1


def test_order_and_product_also_group_across_legs(context):
    by_key = context["entity_cross_references"]["by_key"]

    assert by_key["product_id"][PRODUCT]["source_count"] == 2
    assert set(by_key["product_id"][PRODUCT]["sources"]) == {"graph", "vector"}

    # order_id and customer_id reach the context only from the vector payload, which
    # is precisely what F-01 destroyed: 0 of 8,152 points carried them.
    assert "vector" in by_key["order_id"][ORDER]["sources"]
    assert "vector" in by_key["customer_id"][CUSTOMER]["sources"]


def test_cross_reference_summary_counts_multi_source_entities(context):
    summary = context["entity_cross_references"]["summary"]

    assert summary["seller_id"]["distinct_entities"] == 2
    assert summary["seller_id"]["in_multiple_sources"] == 1


def test_the_grouping_reaches_the_answer_prompt(context):
    """A grouping the answer agent cannot see is not a grouping."""
    text = context["answer_context_text"]

    assert SELLER in text
    assert "appears in graph, sql, vector" in text
    assert OTHER_SELLER not in text.split("Entities linked across sources")[1]


# --------------------------------------------------------------------------------
# Document identifiers as structured fields (the Task 3 gap)
# --------------------------------------------------------------------------------


def test_policy_id_survives_as_a_structured_field(context):
    """The gap found in Task 3.

    Policy IDs reached the prompt only inside the prose of `text_preview`, so the
    evaluator called POL-000025 unsupported while it was sitting in the prompt.
    """
    policies = context["document_evidence"]["results_by_artifact_group"][
        "policy_documents"
    ]

    assert policies
    assert policies[0]["policy_id"] == "POL-000025"


def test_ticket_id_survives_as_a_structured_field(context):
    tickets = context["document_evidence"]["results_by_artifact_group"][
        "support_tickets"
    ]

    assert tickets[0]["ticket_id"] == "TCK-002164"


def test_document_identifiers_are_collected_as_entity_ids(context):
    assert "POL-000025" in context["entity_ids"]["policy_id"]
    assert "TCK-002164" in context["entity_ids"]["ticket_id"]


def test_document_text_reaches_the_context(context):
    """F-02, verified where it actually matters -- in the assembled evidence."""
    tickets = context["document_evidence"]["results_by_artifact_group"][
        "support_tickets"
    ]

    assert tickets[0]["text_preview"].startswith("Customer reports")


# --------------------------------------------------------------------------------
# Skipped legs
# --------------------------------------------------------------------------------


def test_grouping_survives_a_skipped_leg():
    """A SQL-only plan must not break the grouping."""
    report = make_report()
    report["retrieval_results"]["graph"] = {
        "source": "neo4j",
        "intent": None,
        "filters": {},
        "skipped": True,
        "record_count": 0,
        "records": [],
    }

    grouped = build_entity_cross_references(report)["by_key"]["seller_id"]

    assert grouped[SELLER]["sources"] == ["sql", "vector"]
    assert grouped[SELLER]["source_count"] == 2
