from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


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

Required JSON schema:
{{
  "sql_intent": "one allowed SQL intent",
  "graph_intent": "one allowed graph intent",
  "vector_artifact_groups": ["one or more allowed vector artifact groups"],
  "reasoning": "short explanation of why these tools were selected"
}}

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
    sql_intent = plan.get("sql_intent")
    graph_intent = plan.get("graph_intent")
    vector_artifact_groups = plan.get("vector_artifact_groups")
    reasoning = plan.get("reasoning", "")

    if sql_intent not in ALLOWED_SQL_INTENTS:
        raise ValueError(
            f"Invalid SQL intent from Gemini: {sql_intent}. "
            f"Allowed: {ALLOWED_SQL_INTENTS}"
        )

    if graph_intent not in ALLOWED_GRAPH_INTENTS:
        raise ValueError(
            f"Invalid graph intent from Gemini: {graph_intent}. "
            f"Allowed: {ALLOWED_GRAPH_INTENTS}"
        )

    if not isinstance(vector_artifact_groups, list) or not vector_artifact_groups:
        raise ValueError(
            "Gemini planner must return a non-empty vector_artifact_groups list."
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

    return {
        "planner": "gemini_query_planner",
        "planning_mode": "llm_tool_routing",
        "sql_intent": sql_intent,
        "graph_intent": graph_intent,
        "vector_artifact_groups": normalized_vector_groups,
        "reasoning": str(reasoning),
    }


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

    response = client.models.generate_content(
        model=model,
        contents=prompt,
    )

    response_text = getattr(response, "text", None)

    if not response_text:
        raise RuntimeError("Gemini returned an empty planner response.")

    raw_plan = extract_json_from_text(response_text)
    validated_plan = validate_and_normalize_plan(raw_plan)

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
    print(f"SQL intent: {plan['sql_intent']}")
    print(f"Graph intent: {plan['graph_intent']}")
    print(f"Vector groups: {plan['vector_artifact_groups']}")
    print(f"Reasoning: {plan['reasoning']}")
    print(f"Plan saved to: {args.output}")


if __name__ == "__main__":
    main()