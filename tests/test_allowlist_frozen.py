"""M0 freeze: the tool allowlist and the query templates it keys into.

The audit found the allowlist to be the one genuinely sound security control in the
system (8 of 8 adversarial planner prompts rejected). These tests pin its exact
contents so that a later milestone cannot widen it by accident.

Nothing here talks to a database, a network, or Gemini.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from gemini_query_planner import (
    ALLOWED_GRAPH_INTENTS,
    ALLOWED_SQL_INTENTS,
    ALLOWED_VECTOR_GROUPS,
    validate_and_normalize_plan,
)

RETRIEVER = Path(__file__).resolve().parents[1] / "src" / "retrieval" / "hybrid_retriever.py"

FROZEN_SQL_INTENTS = [
    "seller_performance",
    "product_performance",
    "review_intelligence",
    "payment_summary",
    "customer_history",
    "order_summary",
]

FROZEN_GRAPH_INTENTS = [
    "warranty_product_seller_paths",
    "logistics_region_paths",
    "category_policy_guide_paths",
    "seller_ticket_product_paths",
    "customer_ticket_order_product_paths",
]

FROZEN_VECTOR_GROUPS = [
    "support_tickets",
    "logistics_incidents",
    "customer_emails",
    "warranty_claims",
    "policy_documents",
    "troubleshooting_guides",
]


def _template_keys(function_name: str, dict_name: str) -> list[str]:
    """Extract the literal keys of a dict assigned inside a function, without importing.

    hybrid_retriever imports sentence_transformers/torch at module scope, so the
    templates are read from the AST instead.
    """
    tree = ast.parse(RETRIEVER.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == function_name:
            for stmt in node.body:
                if (
                    isinstance(stmt, ast.Assign)
                    and len(stmt.targets) == 1
                    and isinstance(stmt.targets[0], ast.Name)
                    and stmt.targets[0].id == dict_name
                    and isinstance(stmt.value, ast.Dict)
                ):
                    return [k.value for k in stmt.value.keys if isinstance(k, ast.Constant)]
    raise AssertionError(f"{dict_name} not found in {function_name}() of {RETRIEVER}")


def test_sql_allowlist_is_frozen():
    assert ALLOWED_SQL_INTENTS == FROZEN_SQL_INTENTS


def test_graph_allowlist_is_frozen():
    assert ALLOWED_GRAPH_INTENTS == FROZEN_GRAPH_INTENTS


def test_vector_allowlist_is_frozen():
    assert ALLOWED_VECTOR_GROUPS == FROZEN_VECTOR_GROUPS


def test_sql_templates_match_allowlist_exactly():
    """Every allowed intent has a template and no template is unreachable."""
    assert sorted(_template_keys("sql_retrieve", "sql_templates")) == sorted(ALLOWED_SQL_INTENTS)


def test_graph_templates_match_allowlist_exactly():
    assert sorted(_template_keys("graph_retrieve", "cypher_templates")) == sorted(
        ALLOWED_GRAPH_INTENTS
    )


VALID_PLAN = {
    "sql_intent": "seller_performance",
    "graph_intent": "seller_ticket_product_paths",
    "vector_artifact_groups": ["support_tickets", "warranty_claims"],
    "reasoning": "frozen baseline plan",
}


def test_valid_plan_is_accepted_and_normalized():
    out = validate_and_normalize_plan(dict(VALID_PLAN))
    assert out["sql_intent"] == "seller_performance"
    assert out["graph_intent"] == "seller_ticket_product_paths"
    assert out["vector_artifact_groups"] == ["support_tickets", "warranty_claims"]
    assert out["planner"] == "gemini_query_planner"


def test_duplicate_vector_groups_are_deduplicated_in_order():
    plan = dict(VALID_PLAN, vector_artifact_groups=["warranty_claims", "support_tickets", "warranty_claims"])
    assert validate_and_normalize_plan(plan)["vector_artifact_groups"] == [
        "warranty_claims",
        "support_tickets",
    ]


@pytest.mark.parametrize(
    "field, value",
    [
        ("sql_intent", "'; DROP TABLE ecommerce.orders; --"),
        ("sql_intent", "SELECT * FROM pg_shadow"),
        ("sql_intent", None),
        ("graph_intent", "MATCH (n) DETACH DELETE n"),
        ("graph_intent", None),
    ],
)
def test_out_of_allowlist_scalar_intents_are_rejected(field, value):
    plan = dict(VALID_PLAN)
    plan[field] = value
    with pytest.raises(ValueError):
        validate_and_normalize_plan(plan)


@pytest.mark.parametrize(
    "groups",
    [
        ["support_tickets", "pg_catalog"],
        [],
        None,
        "support_tickets",
    ],
)
def test_bad_vector_groups_are_rejected(groups):
    plan = dict(VALID_PLAN, vector_artifact_groups=groups)
    with pytest.raises(ValueError):
        validate_and_normalize_plan(plan)


def test_sql_only_plan_is_currently_unrepresentable():
    """Frozen defect, not a fix.

    A null graph_intent raises, which is why 'Which seller had the highest revenue?'
    crashes the workflow (AUDIT.md P3). M2 changes this; M0 records it.
    """
    plan = dict(VALID_PLAN, graph_intent=None)
    with pytest.raises(ValueError, match="Invalid graph intent"):
        validate_and_normalize_plan(plan)


def test_templates_bind_only_limit():
    """Frozen defect: no template accepts any parameter except the row limit.

    This is F-03 -- the reason a business question and 'purple monkey dishwasher'
    return byte-identical evidence. The test asserts the current state so that M2's
    parameterisation work shows up as a deliberate, visible change to this file.
    """
    source = RETRIEVER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    placeholders = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in {"sql_retrieve", "graph_retrieve"}:
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Constant) and isinstance(stmt.value, str):
                    for token in (":limit", "$limit"):
                        if token in stmt.value:
                            placeholders.add(token)
                    # any other bind placeholder would be a colon/dollar word
                    import re

                    for match in re.findall(r"(?<![:\w]):([a-z_]+)\b", stmt.value):
                        placeholders.add(f":{match}")
                    for match in re.findall(r"\$([a-z_]+)\b", stmt.value):
                        placeholders.add(f"${match}")
    assert placeholders == {":limit", "$limit"}, placeholders
