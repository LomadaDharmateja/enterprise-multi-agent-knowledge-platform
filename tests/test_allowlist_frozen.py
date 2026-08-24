"""M0 freeze: the tool allowlist and the query templates it keys into.

The audit found the allowlist to be the one genuinely sound security control in the
system (8 of 8 adversarial planner prompts rejected). These tests pin its exact
contents so that a later milestone cannot widen it by accident.

Nothing here talks to a database, a network, or Gemini.
"""

from __future__ import annotations

import ast
import re
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
    "answerable": True,
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
        ("sql_intent", "seller_performance; DROP TABLE x"),
        ("graph_intent", "MATCH (n) DETACH DELETE n"),
        ("graph_intent", "CALL apoc.load.json('http://evil')"),
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
        ["support_tickets", "'; DROP TABLE x; --"],
        "support_tickets",
    ],
)
def test_bad_vector_groups_are_rejected(groups):
    plan = dict(VALID_PLAN, vector_artifact_groups=groups)
    with pytest.raises(ValueError):
        validate_and_normalize_plan(plan)


def test_sql_only_plan_is_representable():
    """M2 fix of the defect M0 froze.

    A null graph_intent used to raise, which is why "Which seller had the highest
    revenue?" crashed the workflow (AUDIT.md P3). Gemini was answering correctly --
    the question needs no graph traversal -- and the schema could not represent it.
    """
    plan = dict(VALID_PLAN, graph_intent=None)
    out = validate_and_normalize_plan(plan)
    assert out["sql_intent"] == "seller_performance"
    assert out["graph_intent"] is None
    assert out["answerable"] is True


def test_graph_only_plan_is_representable():
    plan = dict(VALID_PLAN, sql_intent=None)
    out = validate_and_normalize_plan(plan)
    assert out["sql_intent"] is None
    assert out["graph_intent"] == "seller_ticket_product_paths"


def test_vector_only_plan_is_representable():
    plan = dict(VALID_PLAN, sql_intent=None, graph_intent=None)
    out = validate_and_normalize_plan(plan)
    assert out["vector_artifact_groups"] == ["support_tickets", "warranty_claims"]


def test_empty_vector_groups_are_legal_when_another_leg_is_selected():
    plan = dict(VALID_PLAN, vector_artifact_groups=[])
    assert validate_and_normalize_plan(plan)["vector_artifact_groups"] == []


def test_answerable_plan_with_no_legs_at_all_is_rejected():
    """Nullable intents must not become a way to smuggle through an empty plan."""
    plan = dict(
        VALID_PLAN, sql_intent=None, graph_intent=None, vector_artifact_groups=[]
    )
    with pytest.raises(ValueError, match="at least one retrieval leg"):
        validate_and_normalize_plan(plan)


def test_unanswerable_plan_with_no_legs_is_accepted_as_a_refusal():
    plan = dict(
        VALID_PLAN,
        answerable=False,
        refusal_reason="nonsense query",
        sql_intent=None,
        graph_intent=None,
        vector_artifact_groups=[],
    )
    out = validate_and_normalize_plan(plan)
    assert out["answerable"] is False
    assert out["refusal_reason"] == "nonsense query"


def test_templates_bind_exactly_their_declared_parameters():
    """F-03 fix, replacing M0's `test_templates_bind_only_limit`.

    M0 froze the defect: no template accepted any bind parameter except the row
    limit, which is why a business question and "purple monkey dishwasher" returned
    byte-identical evidence. The freeze now runs the other way -- every placeholder
    in a template must be a parameter that template declares, so a typo'd or
    undeclared bind is caught here rather than at runtime.
    """
    from retrieval_parameters import (
        GRAPH_PARAMETER_SPECS,
        SQL_PARAMETER_SPECS,
    )

    source = RETRIEVER.read_text(encoding="utf-8")
    tree = ast.parse(source)

    found: dict[str, set[str]] = {"sql_retrieve": set(), "graph_retrieve": set()}

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in found:
            for stmt in ast.walk(node):
                if isinstance(stmt, ast.Constant) and isinstance(stmt.value, str):
                    for match in re.findall(r"(?<![:\w]):([a-z_]+)\b", stmt.value):
                        found[node.name].add(match)
                    for match in re.findall(r"\$([a-z_]+)\b", stmt.value):
                        found[node.name].add(match)

    sql_declared = {"limit", "sort_by"}
    for spec in SQL_PARAMETER_SPECS.values():
        sql_declared |= set(spec)

    graph_declared = {"limit"}
    for spec in GRAPH_PARAMETER_SPECS.values():
        graph_declared |= set(spec)

    undeclared_sql = found["sql_retrieve"] - sql_declared
    undeclared_graph = found["graph_retrieve"] - graph_declared

    assert not undeclared_sql, f"SQL templates bind undeclared params: {undeclared_sql}"
    assert not undeclared_graph, (
        f"Cypher templates bind undeclared params: {undeclared_graph}"
    )

    # And the defect must not come back: the templates must bind more than the limit.
    assert found["sql_retrieve"] > {"limit"}, "SQL templates still bind only :limit"
    assert found["graph_retrieve"] > {"limit"}, "Cypher templates still bind only $limit"
