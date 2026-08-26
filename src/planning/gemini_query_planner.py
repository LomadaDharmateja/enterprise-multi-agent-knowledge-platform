from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT_FOR_USAGE = Path(__file__).resolve().parents[2]
_OBSERVABILITY_DIR = PROJECT_ROOT_FOR_USAGE / "src" / "observability"

if str(_OBSERVABILITY_DIR) not in sys.path:
    sys.path.append(str(_OBSERVABILITY_DIR))

from llm_usage import timed_call  # noqa: E402

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RETRIEVAL_DIR = PROJECT_ROOT / "src" / "retrieval"

if str(RETRIEVAL_DIR) not in sys.path:
    sys.path.append(str(RETRIEVAL_DIR))

from retrieval_parameters import (  # noqa: E402
    GRAPH_PARAMETER_SPECS,
    SQL_PARAMETER_SPECS,
    SQL_SORT_OPTIONS,
    load_vocabulary,
    resolve_filters,
    resolve_sort_by,
)

PLAN_SCHEMA_VERSION = 2


ALLOWED_SQL_INTENTS = [
    "seller_performance",
    "product_performance",
    "review_intelligence",
    "payment_summary",
    "customer_history",
    "order_summary",
]


ALLOWED_GRAPH_INTENTS = [
    "warranty_product_seller_paths",
    "logistics_region_paths",
    "category_policy_guide_paths",
    "seller_ticket_product_paths",
    "customer_ticket_order_product_paths",
]


ALLOWED_VECTOR_GROUPS = [
    "support_tickets",
    "logistics_incidents",
    "customer_emails",
    "warranty_claims",
    "policy_documents",
    "troubleshooting_guides",
]


def load_settings() -> dict[str, Any]:
    load_dotenv()

    return {
        "gemini_api_key": os.getenv("GEMINI_API_KEY"),
        "gemini_model": os.getenv("GEMINI_MODEL", "Gemini-3.1-Flash-Lite"),
    }


def build_planner_prompt(query: str) -> str:
    return f"""
You are a query planning agent for an enterprise hybrid retrieval system.

Your task is to select which retrieval tools should be used for the user query.

Return ONLY valid JSON.
Do not include Markdown.
Do not include explanation outside JSON.

Available SQL intents:
{json.dumps(ALLOWED_SQL_INTENTS, indent=2)}

SQL intent meanings:
- seller_performance: use for sellers, vendors, seller risk, seller quality, seller metrics.
- product_performance: use for product revenue, product sales, product category performance, product ranking.
- review_intelligence: use for reviews, negative reviews, sentiment, customer complaints, quality complaints, complaint investigation.
- payment_summary: use for payments, refunds, installments, charges, payment behavior.
- customer_history: use for customer lifetime activity, repeat customers, customer history.
- order_summary: use for orders, delivery, late delivery, logistics, shipment, delays.

Available graph intents:
{json.dumps(ALLOWED_GRAPH_INTENTS, indent=2)}

Graph intent meanings:
- warranty_product_seller_paths: warranty claim connected to ticket, product, seller.
- logistics_region_paths: logistics incident connected to order, seller, region.
- category_policy_guide_paths: policy documents and troubleshooting guides connected to category.
- seller_ticket_product_paths: seller connected to support tickets, products, categories.
- customer_ticket_order_product_paths: customer complaint connected to ticket, order, product, category.

Available vector artifact groups:
{json.dumps(ALLOWED_VECTOR_GROUPS, indent=2)}

Vector group meanings:
- support_tickets: customer support cases and complaints.
- logistics_incidents: delivery, carrier, shipment, regional logistics events.
- customer_emails: customer communication messages.
- warranty_claims: warranty, replacement, damaged product claims.
- policy_documents: refund, escalation, seller, delivery, warranty, support policies.
- troubleshooting_guides: operational troubleshooting and investigation guides.

Important routing rules:
1. If the query mentions "complaint", "complaints", "negative complaint", "customer complaint", or "quality complaint", prefer SQL intent "review_intelligence".
2. If the query asks to investigate complaints connected to products, prefer graph intent "customer_ticket_order_product_paths".
3. If the query asks about product sales, product revenue, product ranking, or product performance without complaints, use SQL intent "product_performance".
4. If the query mentions troubleshooting or guidance together with complaints, use vector group "troubleshooting_guides", but do not automatically choose graph intent "category_policy_guide_paths".
5. Use graph intent "category_policy_guide_paths" mainly when the query asks directly for category policies, category guides, or policy-guide relationships.
6. If the query mentions warranty or claims, prefer graph intent "warranty_product_seller_paths".
7. If the query mentions logistics, delivery delay, late delivery, carrier, shipment, or region, prefer SQL intent "order_summary" and graph intent "logistics_region_paths".
8. If the query mentions seller ticket paths, seller complaints, or seller negative issues, prefer graph intent "seller_ticket_product_paths".

Examples:

User query:
Find sellers with negative customer complaints, warranty issues, and relevant support policies

Correct JSON:
{{
  "sql_intent": "seller_performance",
  "graph_intent": "warranty_product_seller_paths",
  "vector_artifact_groups": ["support_tickets", "warranty_claims", "policy_documents"],
  "reasoning": "The query asks about sellers, complaints, warranty issues, and policies. Seller metrics come from SQL, warranty-seller relationships come from the graph, and tickets, warranty claims, and policies are needed from vector retrieval."
}}

User query:
Investigate late delivery logistics incidents by customer region and find troubleshooting guidance

Correct JSON:
{{
  "sql_intent": "order_summary",
  "graph_intent": "logistics_region_paths",
  "vector_artifact_groups": ["logistics_incidents", "troubleshooting_guides"],
  "reasoning": "The query focuses on late delivery and logistics by region, so order summary and logistics-region graph paths are needed, with logistics incidents and troubleshooting guides from vector retrieval."
}}

User query:
Find payment questions, refund policies, and customer support cases

Correct JSON:
{{
  "sql_intent": "payment_summary",
  "graph_intent": "customer_ticket_order_product_paths",
  "vector_artifact_groups": ["support_tickets", "policy_documents"],
  "reasoning": "The query asks about payments, refunds, and support cases. Payment summary provides structured payment evidence, graph paths connect customer tickets to orders and products, and support tickets plus policies provide document evidence."
}}

User query:
Investigate product quality complaints and find troubleshooting procedures

Correct JSON:
{{
  "sql_intent": "review_intelligence",
  "graph_intent": "customer_ticket_order_product_paths",
  "vector_artifact_groups": ["support_tickets", "troubleshooting_guides"],
  "reasoning": "The query is about product quality complaints, so review intelligence is the correct SQL route. Complaint investigation should trace customer tickets to orders and products. Troubleshooting guides are useful as document evidence."
}}

User query:
Show seller ticket paths for negative complaints and product issues

Correct JSON:
{{
  "sql_intent": "seller_performance",
  "graph_intent": "seller_ticket_product_paths",
  "vector_artifact_groups": ["support_tickets"],
  "reasoning": "The query asks specifically for seller ticket paths involving negative complaints and product issues, so seller performance and seller-ticket-product graph paths are required, with support tickets as document evidence."
}}

Filter parameters you may supply, per SQL intent:
{json.dumps({k: sorted(v) for k, v in SQL_PARAMETER_SPECS.items()}, indent=2)}

Filter parameters you may supply, per graph intent:
{json.dumps({k: sorted(v) for k, v in GRAPH_PARAMETER_SPECS.items()}, indent=2)}

Sort options, per SQL intent (pick the one the question actually asks to rank by):
{json.dumps(SQL_SORT_OPTIONS, indent=2)}

Rules for values:
- Supply ONLY values the question actually states or clearly implies. Omit a filter
  rather than guessing it. An omitted filter is correct; an invented one is not.
- Do NOT write SQL, Cypher, column names, operators, or any query fragment. Supply
  values only. Anything that is not a plain value will be discarded.
- Entity IDs must be copied verbatim from the question. Never invent one.
- Dates must be ISO format, YYYY-MM-DD.
- Categories may be given in English or Portuguese; they will be resolved.
- For sort_by, choose from the list above for the SQL intent you selected. A question
  about complaints, reliability or the worst performers should rank by a rate or a
  score, not by a volume. A question about revenue or size should rank by the money
  or count column.

Answerability:
- If the query is not a business question about this data -- nonsense, unrelated, or
  with no answerable content -- set "answerable" to false, set all three routes to
  null or empty, and give a short "refusal_reason". Do NOT pick a plausible-looking
  route for a question you cannot answer.
- A query needing only structured facts may set "graph_intent" to null. A query
  needing only relationships may set "sql_intent" to null. Do not select a route you
  do not need.

Required JSON schema:
{{
  "answerable": true,
  "refusal_reason": null,
  "sql_intent": "one allowed SQL intent, or null",
  "graph_intent": "one allowed graph intent, or null",
  "vector_artifact_groups": ["zero or more allowed vector artifact groups"],
  "sql_filters": {{}},
  "sql_sort_by": "one allowed sort option for the chosen SQL intent, or null",
  "graph_filters": {{}},
  "reasoning": "short explanation of why these tools and values were selected"
}}

Worked example of values (routes omitted for brevity):

User query: List the highest-value orders placed in 2018
{{"answerable": true, "sql_intent": "order_summary", "graph_intent": null,
  "vector_artifact_groups": [],
  "sql_filters": {{"date_from": "2018-01-01", "date_to": "2018-12-31"}},
  "sql_sort_by": "total_payment_value", "graph_filters": {{}}}}

User query: What is the capital of Portugal?
{{"answerable": false, "refusal_reason": "The query is general knowledge, not a
  question about the seller, order, review or support data this system holds.",
  "sql_intent": null, "graph_intent": null, "vector_artifact_groups": [],
  "sql_filters": {{}}, "graph_filters": {{}}}}

User query:
{query}
""".strip()


def extract_json_from_text(text: str) -> dict[str, Any]:
    cleaned = text.strip()

    if cleaned.startswith("```"):
        cleaned = cleaned.replace("```json", "").replace("```", "").strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")

    if start == -1 or end == -1:
        raise ValueError(f"Could not find JSON object in Gemini response: {text}")

    json_text = cleaned[start : end + 1]

    return json.loads(json_text)


def validate_and_normalize_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Validate the ROUTES. No database, no vocabulary -- shape only.

    M2 change: a null intent is now a legal answer. "Which seller had the highest
    revenue?" needs no graph traversal, and requiring a non-null graph intent made
    that answer unrepresentable, which is why the question crashed the workflow.

    The allowlist is NOT loosened: a non-null intent that is not on it still raises,
    and an answerable plan that selects no leg at all still raises.
    """
    answerable = plan.get("answerable", True)

    if not isinstance(answerable, bool):
        answerable = str(answerable).strip().lower() != "false"

    sql_intent = plan.get("sql_intent")
    graph_intent = plan.get("graph_intent")
    vector_artifact_groups = plan.get("vector_artifact_groups")
    reasoning = plan.get("reasoning", "")

    if sql_intent is not None and sql_intent not in ALLOWED_SQL_INTENTS:
        raise ValueError(
            f"Invalid SQL intent from Gemini: {sql_intent}. "
            f"Allowed: {ALLOWED_SQL_INTENTS}"
        )

    if graph_intent is not None and graph_intent not in ALLOWED_GRAPH_INTENTS:
        raise ValueError(
            f"Invalid graph intent from Gemini: {graph_intent}. "
            f"Allowed: {ALLOWED_GRAPH_INTENTS}"
        )

    if vector_artifact_groups is None:
        vector_artifact_groups = []

    if not isinstance(vector_artifact_groups, list):
        raise ValueError(
            "Gemini planner must return vector_artifact_groups as a list."
        )

    normalized_vector_groups = []

    for group in vector_artifact_groups:
        if group not in ALLOWED_VECTOR_GROUPS:
            raise ValueError(
                f"Invalid vector artifact group from Gemini: {group}. "
                f"Allowed: {ALLOWED_VECTOR_GROUPS}"
            )

        if group not in normalized_vector_groups:
            normalized_vector_groups.append(group)

    if answerable and not (sql_intent or graph_intent or normalized_vector_groups):
        raise ValueError(
            "An answerable plan must select at least one retrieval leg. "
            "A plan with no legs must set answerable=false with a refusal_reason."
        )

    refusal_reason = plan.get("refusal_reason")

    if not answerable and not refusal_reason:
        refusal_reason = "The planner selected no retrieval route for this query."

    return {
        "planner": "gemini_query_planner",
        "planning_mode": "llm_tool_routing",
        "plan_schema_version": PLAN_SCHEMA_VERSION,
        "answerable": answerable,
        "refusal_reason": refusal_reason if not answerable else None,
        "sql_intent": sql_intent,
        "graph_intent": graph_intent,
        "vector_artifact_groups": normalized_vector_groups,
        "reasoning": str(reasoning),
    }


def resolve_plan_parameters(
    normalized_plan: dict[str, Any],
    raw_plan: dict[str, Any],
    vocabulary: dict[str, Any],
) -> dict[str, Any]:
    """Attach the VALUES to the routes, after checking every one of them.

    Nothing the model proposed reaches a database until it has been matched against
    the template's parameter allowlist and the live vocabulary. Whatever does not
    resolve is recorded in `dropped_filters` -- a filter that vanishes silently is
    the F-06 failure mode, where the answer is grounded on an empty table that looks
    exactly like a filtered one.
    """
    sql_intent = normalized_plan["sql_intent"]
    graph_intent = normalized_plan["graph_intent"]

    dropped: list[dict[str, Any]] = []

    sql_filters, sql_dropped, sql_proposed = resolve_filters(
        leg="sql",
        intent=sql_intent,
        proposed=raw_plan.get("sql_filters"),
        vocabulary=vocabulary,
    )
    dropped.extend(sql_dropped)

    graph_filters, graph_dropped, graph_proposed = resolve_filters(
        leg="graph",
        intent=graph_intent,
        proposed=raw_plan.get("graph_filters"),
        vocabulary=vocabulary,
    )
    dropped.extend(graph_dropped)

    proposed_sort = raw_plan.get("sql_sort_by")
    sort_by = resolve_sort_by(sql_intent, proposed_sort, dropped)

    # sort_by is deliberately NOT counted as a filter. An unresolvable sort key falls
    # back to the template's default, which is a sensible ranking; an unresolvable
    # filter silently widens the result set, which is the failure this count exists
    # to detect. Counting the sort here made a question with no filters at all look
    # like a question whose every filter was dropped.
    proposed_count = sql_proposed + graph_proposed

    plan = dict(normalized_plan)
    plan["sql_plan"] = (
        {"intent": sql_intent, "filters": sql_filters, "sort_by": sort_by}
        if sql_intent
        else None
    )
    plan["graph_plan"] = (
        {"intent": graph_intent, "filters": graph_filters} if graph_intent else None
    )
    plan["vector_plan"] = {
        "artifact_groups": normalized_plan["vector_artifact_groups"]
    }
    plan["dropped_filters"] = dropped
    plan["filters_proposed"] = proposed_count
    plan["filters_accepted"] = len(sql_filters) + len(graph_filters)

    return plan


def build_vocabulary_engine():
    """A read-only connection used only to read the filter vocabulary."""
    from sqlalchemy import create_engine

    load_dotenv()

    url = (
        f"postgresql+psycopg2://{os.getenv('POSTGRES_USER', 'enterprise_user')}:"
        f"{os.getenv('POSTGRES_PASSWORD', "")}@"
        f"{os.getenv('POSTGRES_HOST', 'localhost')}:"
        f"{os.getenv('POSTGRES_PORT', '5432')}/"
        f"{os.getenv('POSTGRES_DB', 'enterprise_ai')}"
    )

    return create_engine(url)


def call_gemini_for_plan(
    query: str,
    model: str,
    api_key: str | None,
) -> dict[str, Any]:
    if not api_key:
        raise ValueError(
            "GEMINI_API_KEY is missing. Add GEMINI_API_KEY to your .env file."
        )

    try:
        from google import genai
    except ImportError as exc:
        raise ImportError(
            "google-genai is not installed. Run: pip install google-genai"
        ) from exc

    prompt = build_planner_prompt(query)

    client = genai.Client(api_key=api_key)

    with timed_call("planner_agent", model, len(prompt)) as call:
        response = client.models.generate_content(
            model=model,
            contents=prompt,
        )
        call["response"] = response

    response_text = getattr(response, "text", None)

    if not response_text:
        raise RuntimeError("Gemini returned an empty planner response.")

    raw_plan = extract_json_from_text(response_text)
    validated_plan = validate_and_normalize_plan(raw_plan)

    engine = build_vocabulary_engine()

    try:
        vocabulary = load_vocabulary(engine)
    finally:
        engine.dispose()

    validated_plan = resolve_plan_parameters(validated_plan, raw_plan, vocabulary)

    validated_plan["generated_at"] = datetime.now(timezone.utc).isoformat()
    validated_plan["model"] = model
    validated_plan["query"] = query
    validated_plan["raw_response"] = response_text

    return validated_plan


def plan_query_with_gemini(query: str) -> dict[str, Any]:
    settings = load_settings()

    return call_gemini_for_plan(
        query=query,
        model=settings["gemini_model"],
        api_key=settings["gemini_api_key"],
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Plan enterprise retrieval routes using Gemini."
    )

    parser.add_argument(
        "--query",
        type=str,
        required=True,
        help="Natural-language business query.",
    )

    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/gemini_query_plan.json"),
        help="Path to save Gemini query plan.",
    )

    return parser.parse_args()


def main() -> None:
    args = parse_args()

    print("Planning query with Gemini...")
    plan = plan_query_with_gemini(args.query)

    args.output.parent.mkdir(parents=True, exist_ok=True)

    with args.output.open("w", encoding="utf-8") as file:
        json.dump(plan, file, indent=2, ensure_ascii=False, default=str)

    print("\nGemini Query Planning Completed")
    print("-------------------------------")
    print(f"Planner: {plan['planner']}")
    print(f"Model: {plan['model']}")
    print(f"Answerable: {plan['answerable']}")
    print(f"SQL intent: {plan['sql_intent']}")
    print(f"Graph intent: {plan['graph_intent']}")
    print(f"Vector groups: {plan['vector_artifact_groups']}")
    print(f"SQL plan: {plan.get('sql_plan')}")
    print(f"Graph plan: {plan.get('graph_plan')}")
    print(f"Dropped filters: {plan.get('dropped_filters')}")
    print(f"Reasoning: {plan['reasoning']}")
    print(f"Plan saved to: {args.output}")


if __name__ == "__main__":
    main()