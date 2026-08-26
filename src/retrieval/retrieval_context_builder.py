from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from hybrid_retriever import run_hybrid_retrieval


ENTITY_KEYS = [
    "customer_id",
    "customer_unique_id",
    "order_id",
    "product_id",
    "seller_id",
    "ticket_id",
    "claim_id",
    "incident_id",
    "review_id",
    "category_id",
    "region_id",
    "policy_id",
    "guide_id",
    "email_id",
]

# The IDs that can actually join a vector hit to a SQL row or a graph node. F-01 was
# that none of them survived ingest, so no cross-source grouping was possible at all.
JOIN_KEYS = ["seller_id", "order_id", "product_id", "customer_id"]


IMPORTANT_SQL_FIELDS = [
    "seller_id",
    "seller_state",
    "seller_city",
    "total_orders",
    "total_items_sold",
    "total_item_revenue",
    "avg_review_score",
    "review_count",
    "late_delivery_orders",
    "order_id",
    "customer_id",
    "customer_state",
    "customer_city",
    "order_status",
    "delivery_status",
    "is_late_delivery",
    "delivery_delay_days",
    "total_payment_value",
    "review_score",
    "sentiment_label",
    "is_negative_review",
    "payment_types",
    "max_payment_installments",
    "product_id",
    "product_category_name_english",
    "total_product_revenue",
]


IMPORTANT_GRAPH_FIELDS = [
    "seller_id",
    "seller_state",
    "ticket_id",
    "issue_type",
    "severity",
    "claim_id",
    "claim_status",
    "incident_id",
    "incident_type",
    "order_id",
    "delivery_delay_days",
    "product_id",
    "category",
    "category_id",
    "region_state",
    "region_city",
    "policy_document_id",
    "policy_id",
    "policy_topic",
    "applicability_scope",
    "policy_title",
    "guide_id",
    "guide_title",
    "customer_id",
    "customer_state",
]


IMPORTANT_VECTOR_FIELDS = [
    "score",
    "artifact_group",
    "artifact_type",
    "artifact_id",
    "title",
    "issue_type",
    "severity",
    "status",
    "policy_topic",
    "customer_id",
    "order_id",
    "product_id",
    "seller_id",
    "ticket_id",
    "claim_id",
    "incident_id",
    "email_id",
    "policy_id",
    "guide_id",
    "category_id",
    "region_id",
    "text_preview",
]


def stringify_value(value: Any) -> str:
    if value is None:
        return ""

    if isinstance(value, (str, int, float, bool)):
        return str(value)

    return json.dumps(value, default=str, ensure_ascii=False)


def compact_record(
    record: dict[str, Any],
    preferred_fields: list[str],
    max_text_length: int = 350,
) -> dict[str, Any]:
    compact = {}

    for field in preferred_fields:
        if field not in record:
            continue

        value = record.get(field)

        if value is None:
            continue

        if isinstance(value, str) and len(value) > max_text_length:
            value = value[:max_text_length].strip() + "..."

        compact[field] = value

    if not compact:
        for key, value in record.items():
            if value is None:
                continue

            if isinstance(value, str) and len(value) > max_text_length:
                value = value[:max_text_length].strip() + "..."

            compact[key] = value

    return compact


def collect_entity_ids_from_record(
    record: dict[str, Any],
    entity_store: dict[str, set[str]],
) -> None:
    for key in ENTITY_KEYS:
        value = record.get(key)

        if value is None:
            continue

        if isinstance(value, list):
            for item in value:
                if item is not None:
                    entity_store[key].add(stringify_value(item))
        else:
            entity_store[key].add(stringify_value(value))


def collect_entity_ids(report: dict[str, Any]) -> dict[str, list[str]]:
    entity_store: dict[str, set[str]] = {
        key: set()
        for key in ENTITY_KEYS
    }

    sql_records = report["retrieval_results"]["sql"]["records"]
    graph_records = report["retrieval_results"]["graph"]["records"]
    vector_results_by_group = report["retrieval_results"]["vector"]["results_by_artifact_group"]

    for record in sql_records:
        collect_entity_ids_from_record(record, entity_store)

    for record in graph_records:
        collect_entity_ids_from_record(record, entity_store)

    for records in vector_results_by_group.values():
        for record in records:
            collect_entity_ids_from_record(record, entity_store)

    return {
        key: sorted(values)[:25]
        for key, values in entity_store.items()
        if values
    }


def record_reference(source: str, record: dict[str, Any]) -> str:
    """A short, human-checkable handle for one evidence record."""
    for key in (
        "artifact_id",
        "ticket_id",
        "claim_id",
        "incident_id",
        "email_id",
        "policy_id",
        "policy_document_id",
        "guide_id",
        "review_id",
        "order_id",
        "seller_id",
        "product_id",
        "customer_unique_id",
        "customer_id",
    ):
        value = record.get(key)

        if value:
            return f"{source}:{key}={stringify_value(value)}"

    return f"{source}:record"


def build_entity_cross_references(
    report: dict[str, Any],
    max_entities_per_key: int = 10,
    max_references_per_entity: int = 5,
) -> dict[str, Any]:
    """Group evidence by entity ID, showing which sources agree on it.

    This is the point of F-01. A flat list of IDs tells the answer agent nothing about
    which SQL row belongs with which document; a vector hit that joins to a SQL row is
    only useful if the join is visible. Entities seen in more than one leg are listed
    first, because those are the ones that carry a cross-source claim.
    """
    results = report["retrieval_results"]

    legs = {
        "sql": results["sql"]["records"],
        "graph": results["graph"]["records"],
    }

    vector_records = []

    for group_records in results["vector"]["results_by_artifact_group"].values():
        vector_records.extend(group_records)

    legs["vector"] = vector_records

    grouped: dict[str, dict[str, dict[str, list[str]]]] = {key: {} for key in JOIN_KEYS}

    for source, records in legs.items():
        for record in records:
            for key in JOIN_KEYS:
                value = record.get(key)

                if not value:
                    continue

                entity_id = stringify_value(value)
                entry = grouped[key].setdefault(entity_id, {})
                references = entry.setdefault(source, [])

                reference = record_reference(source, record)

                if (
                    reference not in references
                    and len(references) < max_references_per_entity
                ):
                    references.append(reference)

    cross_references: dict[str, Any] = {}
    summary: dict[str, Any] = {}

    for key, entities in grouped.items():
        if not entities:
            continue

        # Multi-source entities first, then by how much evidence they carry.
        ranked = sorted(
            entities.items(),
            key=lambda item: (-len(item[1]), -sum(len(v) for v in item[1].values()), item[0]),
        )

        kept = dict(ranked[:max_entities_per_key])

        cross_references[key] = {
            entity_id: {
                "sources": sorted(sources),
                "source_count": len(sources),
                "references": sorted(r for refs in sources.values() for r in refs),
            }
            for entity_id, sources in kept.items()
        }

        summary[key] = {
            "distinct_entities": len(entities),
            "in_multiple_sources": sum(1 for _, s in entities.items() if len(s) > 1),
        }

    return {"by_key": cross_references, "summary": summary}


def build_sql_evidence(
    sql_result: dict[str, Any],
    max_records: int,
) -> dict[str, Any]:
    records = [
        compact_record(record, IMPORTANT_SQL_FIELDS)
        for record in sql_result["records"][:max_records]
    ]

    return {
        "source": "postgresql",
        "retrieval_role": "structured_business_facts",
        "intent": sql_result["intent"],
        "total_records_returned": sql_result["record_count"],
        "records_in_context": len(records),
        "evidence": records,
    }


def build_graph_evidence(
    graph_result: dict[str, Any],
    max_records: int,
) -> dict[str, Any]:
    records = [
        compact_record(record, IMPORTANT_GRAPH_FIELDS)
        for record in graph_result["records"][:max_records]
    ]

    return {
        "source": "neo4j",
        "retrieval_role": "connected_entity_reasoning",
        "intent": graph_result["intent"],
        "total_records_returned": graph_result["record_count"],
        "records_in_context": len(records),
        "evidence": records,
    }


def build_document_evidence(
    vector_result: dict[str, Any],
    max_records_per_group: int,
) -> dict[str, Any]:
    grouped_evidence = {}

    for artifact_group, records in vector_result["results_by_artifact_group"].items():
        grouped_evidence[artifact_group] = [
            compact_record(record, IMPORTANT_VECTOR_FIELDS)
            for record in records[:max_records_per_group]
        ]

    return {
        "source": "qdrant",
        "retrieval_role": "semantic_document_retrieval",
        "artifact_groups": vector_result["artifact_groups"],
        "total_records_returned": vector_result["record_count"],
        "results_by_artifact_group": grouped_evidence,
    }


def build_source_summary(
    sql_evidence: dict[str, Any],
    graph_evidence: dict[str, Any],
    document_evidence: dict[str, Any],
    sql_result: dict[str, Any] | None = None,
    graph_result: dict[str, Any] | None = None,
    vector_result: dict[str, Any] | None = None,
) -> dict[str, Any]:
    sql_result = sql_result or {}
    graph_result = graph_result or {}

    return {
        "sql": {
            "source": "postgresql",
            "intent": sql_evidence["intent"],
            "records": sql_evidence["total_records_returned"],
            "role": sql_evidence["retrieval_role"],
            "skipped": bool(sql_result.get("skipped")),
            "filters": sql_result.get("filters", {}),
            "sort_by": sql_result.get("sort_by"),
        },
        "graph": {
            "source": "neo4j",
            "intent": graph_evidence["intent"],
            "records": graph_evidence["total_records_returned"],
            "role": graph_evidence["retrieval_role"],
            "skipped": bool(graph_result.get("skipped")),
            "filters": graph_result.get("filters", {}),
        },
        "vector": {
            "source": "qdrant",
            "artifact_groups": document_evidence["artifact_groups"],
            "records": document_evidence["total_records_returned"],
            "role": document_evidence["retrieval_role"],
            "id_lookups": (vector_result or {}).get("id_lookups", []),
            "id_lookup_hits": (vector_result or {}).get("id_lookup_hits", 0),
        },
    }


def build_recommended_next_actions(
    sql_intent: str,
    graph_intent: str,
    vector_groups: list[str],
) -> list[str]:
    actions = []

    if sql_intent == "seller_performance":
        actions.append("Review high-risk sellers using revenue, review score, and late-delivery metrics.")

    if sql_intent == "order_summary":
        actions.append("Inspect late or delayed orders and compare delivery delay patterns.")

    if sql_intent == "review_intelligence":
        actions.append("Analyze negative reviews and connect them to products, sellers, and support cases.")

    if sql_intent == "payment_summary":
        actions.append("Check payment patterns, refunds, installments, and high-value orders.")

    if graph_intent == "warranty_product_seller_paths":
        actions.append("Trace warranty claims from ticket to product to seller.")

    if graph_intent == "logistics_region_paths":
        actions.append("Trace logistics incidents through affected orders, sellers, and regions.")

    if graph_intent == "seller_ticket_product_paths":
        actions.append("Trace seller-related support tickets to affected products and categories.")

    if graph_intent == "customer_ticket_order_product_paths":
        actions.append("Trace customer complaints through tickets, orders, products, and categories.")

    if "policy_documents" in vector_groups:
        actions.append("Use retrieved policy documents to support escalation or refund decisions.")

    if "troubleshooting_guides" in vector_groups:
        actions.append("Use retrieved troubleshooting guides to propose operational investigation steps.")

    if not actions:
        actions.append("Review retrieved SQL, graph, and document evidence before producing a final answer.")

    return actions


def build_context_text(context: dict[str, Any]) -> str:
    lines = []

    lines.append(f"Business question: {context['business_question']}")
    lines.append("")

    lines.append("Source summary:")

    sql_summary = context["source_summary"]["sql"]
    graph_summary = context["source_summary"]["graph"]

    if sql_summary.get("skipped"):
        lines.append("- PostgreSQL: not selected by the plan; no SQL evidence gathered")
    else:
        lines.append(
            f"- PostgreSQL intent: {sql_summary['intent']} "
            f"({sql_summary['records']} records); "
            f"filters={sql_summary.get('filters') or 'none'}; "
            f"ranked by {sql_summary.get('sort_by')}"
        )

    if graph_summary.get("skipped"):
        lines.append("- Neo4j: not selected by the plan; no graph evidence gathered")
    else:
        lines.append(
            f"- Neo4j intent: {graph_summary['intent']} "
            f"({graph_summary['records']} records); "
            f"filters={graph_summary.get('filters') or 'none'}"
        )

    lines.append(
        f"- Qdrant artifact groups: {context['source_summary']['vector']['artifact_groups']} "
        f"({context['source_summary']['vector']['records']} records)"
    )
    lines.append("")

    # The dangerous case is not an error, it is a question whose filters all failed
    # to resolve: the query silently degrades to the unfiltered one F-03 was about,
    # and the resulting table looks exactly like a filtered one. Say so in the prompt.
    unavailable = context.get("unavailable_legs") or []

    if unavailable:
        lines.append("DEGRADED RETRIEVAL -- one or more sources were unavailable:")
        for leg in unavailable:
            lines.append(f"- {leg['source']} ({leg.get('dependency')}): {leg.get('reason')}")
        lines.append(
            "State plainly in the answer which source was unavailable and that the "
            "answer is based on the remaining sources only. Do not present the "
            "evidence as complete."
        )
        lines.append("")

    evidence_quality = context.get("evidence_quality") or {}

    if evidence_quality:
        lines.append(f"Evidence confidence: {evidence_quality.get('confidence')}")

        for reason in evidence_quality.get("reasons", []):
            lines.append(f"- {reason}")

        if evidence_quality.get("all_filters_dropped"):
            lines.append(
                "- TREAT THIS EVIDENCE AS UNFILTERED. State in the answer that the "
                "question could not be narrowed to the entities it named, and do not "
                "present these rows as if they were selected for this question."
            )

        lines.append("")

    lines.append("Recommended next actions:")
    for action in context["recommended_next_actions"]:
        lines.append(f"- {action}")

    lines.append("")

    lines.append("Key entity IDs:")
    for key, values in context["entity_ids"].items():
        lines.append(f"- {key}: {', '.join(values[:10])}")

    cross_references = (context.get("entity_cross_references") or {}).get("by_key", {})

    if cross_references:
        lines.append("")
        lines.append(
            "Entities linked across sources (an entity listed under two or more "
            "sources is supported by evidence from each of them):"
        )

        for key, entities in cross_references.items():
            for entity_id, detail in entities.items():
                if detail["source_count"] < 2:
                    continue

                lines.append(
                    f"- {key}={entity_id} appears in "
                    f"{', '.join(detail['sources'])}: "
                    f"{'; '.join(detail['references'][:5])}"
                )

    return "\n".join(lines)


def build_retrieval_context(
    query: str,
    raw_report_path: Path,
    context_output_path: Path,
    sql_limit: int,
    graph_limit: int,
    vector_limit: int,
    max_records_per_section: int,
    route_plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    print("Running hybrid retrieval...")
    raw_report = run_hybrid_retrieval(
        query=query,
        output_path=raw_report_path,
        sql_limit=sql_limit,
        graph_limit=graph_limit,
        vector_limit=vector_limit,
        route_plan=route_plan,
    )

    sql_result = raw_report["retrieval_results"]["sql"]
    graph_result = raw_report["retrieval_results"]["graph"]
    vector_result = raw_report["retrieval_results"]["vector"]

    print("Building SQL evidence context...")
    sql_evidence = build_sql_evidence(
        sql_result=sql_result,
        max_records=max_records_per_section,
    )

    print("Building graph evidence context...")
    graph_evidence = build_graph_evidence(
        graph_result=graph_result,
        max_records=max_records_per_section,
    )

    print("Building document evidence context...")
    document_evidence = build_document_evidence(
        vector_result=vector_result,
        max_records_per_group=max_records_per_section,
    )

    source_summary = build_source_summary(
        sql_evidence=sql_evidence,
        graph_evidence=graph_evidence,
        document_evidence=document_evidence,
        sql_result=sql_result,
        graph_result=graph_result,
        vector_result=vector_result,
    )

    entity_ids = collect_entity_ids(raw_report)
    entity_cross_references = build_entity_cross_references(raw_report)

    recommended_next_actions = build_recommended_next_actions(
        sql_intent=sql_result["intent"],
        graph_intent=graph_result["intent"],
        vector_groups=vector_result["artifact_groups"],
    )

    context = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "business_question": query,
        "route_plan": route_plan,
        "summary": {
            "overall_status": "PASS",
            "context_sections": [
                "source_summary",
                "sql_evidence",
                "graph_evidence",
                "document_evidence",
                "entity_ids",
                "entity_cross_references",
                "recommended_next_actions",
                "answer_context_text",
            ],
            "sql_records_in_context": sql_evidence["records_in_context"],
            "graph_records_in_context": graph_evidence["records_in_context"],
            "document_artifact_groups": document_evidence["artifact_groups"],
        },
        "source_summary": source_summary,
        "evidence_quality": raw_report.get("summary", {}).get("evidence_quality", {}),
        "unavailable_legs": raw_report.get("summary", {}).get("unavailable_legs", []),
        "sql_evidence": sql_evidence,
        "graph_evidence": graph_evidence,
        "document_evidence": document_evidence,
        "entity_ids": entity_ids,
        "entity_cross_references": entity_cross_references,
        "recommended_next_actions": recommended_next_actions,
    }

    context["answer_context_text"] = build_context_text(context)

    context_output_path.parent.mkdir(parents=True, exist_ok=True)

    with context_output_path.open("w", encoding="utf-8") as file:
        json.dump(context, file, indent=2, default=str, ensure_ascii=False)

    return context


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build an answer-ready retrieval context from hybrid retrieval results."
    )

    parser.add_argument(
        "--query",
        type=str,
        required=True,
        help="Natural-language business query.",
    )

    parser.add_argument(
        "--raw-output",
        type=Path,
        default=Path("reports/hybrid_retrieval_raw_report.json"),
        help="Path to save raw hybrid retrieval output.",
    )

    parser.add_argument(
        "--context-output",
        type=Path,
        default=Path("reports/retrieval_context.json"),
        help="Path to save answer-ready retrieval context.",
    )

    parser.add_argument(
        "--sql-limit",
        type=int,
        default=10,
        help="Number of SQL records to retrieve.",
    )

    parser.add_argument(
        "--graph-limit",
        type=int,
        default=10,
        help="Number of graph records to retrieve.",
    )

    parser.add_argument(
        "--vector-limit",
        type=int,
        default=5,
        help="Number of vector records per artifact group.",
    )

    parser.add_argument(
        "--max-records-per-section",
        type=int,
        default=5,
        help="Maximum records to include in each final context section.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    context = build_retrieval_context(
        query=args.query,
        raw_report_path=args.raw_output,
        context_output_path=args.context_output,
        sql_limit=args.sql_limit,
        graph_limit=args.graph_limit,
        vector_limit=args.vector_limit,
        max_records_per_section=args.max_records_per_section,
    )

    print("\nRetrieval Context Build Completed")
    print("---------------------------------")
    print(f"Overall status: {context['summary']['overall_status']}")
    print(f"Question: {context['business_question']}")
    print(f"Context saved to: {args.context_output}")
    print(f"Raw retrieval saved to: {args.raw_output}")

    print("\nSource summary:")
    print(f"SQL intent: {context['source_summary']['sql']['intent']}")
    print(f"Graph intent: {context['source_summary']['graph']['intent']}")
    print(f"Vector groups: {context['source_summary']['vector']['artifact_groups']}")

    print("\nRecommended next actions:")
    for action in context["recommended_next_actions"]:
        print(f"- {action}")

    print("\nEntity ID groups:")
    for key, values in context["entity_ids"].items():
        print(f"{key}: {len(values)}")


if __name__ == "__main__":
    main()