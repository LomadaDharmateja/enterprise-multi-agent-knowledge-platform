"""Unit tests for the plan parameter contract (F-03).

No database, no Gemini -- these run in CI. The vocabulary is supplied directly so
the resolution rules can be tested without a stack.

The property under test is the one the whole design rests on: the planner proposes
values, and nothing it proposes reaches a query until it has been matched against the
template's parameter allowlist and a known vocabulary.
"""

from __future__ import annotations

import pytest

from retrieval_parameters import (
    GRAPH_PARAMETER_SPECS,
    SQL_DEFAULT_SORT,
    SQL_PARAMETER_SPECS,
    SQL_SORT_OPTIONS,
    build_bind_parameters,
    resolve_filters,
    resolve_sort_by,
)

VOCABULARY = {
    "category_by_any_name": {
        "moveis_decoracao": "moveis_decoracao",
        "furniture_decor": "moveis_decoracao",
        "cama_mesa_banho": "cama_mesa_banho",
        "bed_bath_table": "cama_mesa_banho",
    },
    "english_by_category_id": {"moveis_decoracao": "furniture_decor"},
    "seller_states": ["SP", "RJ", "MG"],
    "customer_states": ["SP", "RJ", "MG"],
    "order_statuses": ["delivered", "canceled", "shipped"],
    "severities": ["critical", "high", "medium", "low"],
    "issue_types": ["product_quality_complaint", "delayed_delivery"],
    "incident_types": ["carrier_backlog"],
    "claim_statuses": ["approved", "rejected"],
    "policy_topics": ["late_delivery_compensation"],
    "sentiment_labels": ["negative", "neutral", "positive"],
    "delivery_statuses": ["delivered", "not_delivered"],
}

REAL_SELLER_ID = "06a2c3af7b3aee5d69171b0e14f0ee87"


# --------------------------------------------------------------------------------
# Values that resolve
# --------------------------------------------------------------------------------


def test_state_and_threshold_values_are_accepted():
    accepted, dropped, proposed = resolve_filters(
        leg="sql",
        intent="seller_performance",
        proposed={"seller_states": ["SP"], "max_avg_review_score": 3.5},
        vocabulary=VOCABULARY,
    )
    assert accepted == {"seller_states": ["SP"], "max_avg_review_score": 3.5}
    assert dropped == []
    assert proposed == 2


def test_category_resolves_from_either_language():
    for supplied in ("furniture_decor", "moveis_decoracao", "Furniture_Decor"):
        accepted, dropped, _ = resolve_filters(
            leg="sql",
            intent="product_performance",
            proposed={"product_categories": [supplied]},
            vocabulary=VOCABULARY,
        )
        assert accepted == {"product_categories": ["moveis_decoracao"]}, supplied
        assert dropped == []


def test_duplicate_values_are_collapsed():
    accepted, _, _ = resolve_filters(
        leg="sql",
        intent="product_performance",
        proposed={"product_categories": ["furniture_decor", "moveis_decoracao"]},
        vocabulary=VOCABULARY,
    )
    assert accepted == {"product_categories": ["moveis_decoracao"]}


# --------------------------------------------------------------------------------
# Values that do not resolve -- dropped, and recorded
# --------------------------------------------------------------------------------


def test_unknown_category_is_dropped_and_recorded():
    """The M2 requirement: an invented entity name never reaches the database."""
    accepted, dropped, proposed = resolve_filters(
        leg="sql",
        intent="product_performance",
        proposed={"product_categories": ["artisanal moon cheese"]},
        vocabulary=VOCABULARY,
    )
    assert accepted == {}
    assert proposed == 1
    assert len(dropped) == 1
    assert dropped[0]["key"] == "product_categories"
    assert dropped[0]["value"] == "artisanal moon cheese"
    assert "category vocabulary" in dropped[0]["reason"]


def test_partial_resolution_keeps_the_good_value_and_records_the_bad():
    accepted, dropped, _ = resolve_filters(
        leg="sql",
        intent="product_performance",
        proposed={"product_categories": ["furniture_decor", "not a category"]},
        vocabulary=VOCABULARY,
    )
    assert accepted == {"product_categories": ["moveis_decoracao"]}
    assert [d["value"] for d in dropped] == ["not a category"]


def test_a_filter_the_template_does_not_declare_is_dropped():
    accepted, dropped, _ = resolve_filters(
        leg="sql",
        intent="seller_performance",
        proposed={"product_categories": ["furniture_decor"]},
        vocabulary=VOCABULARY,
    )
    assert accepted == {}
    assert dropped[0]["reason"] == "not a parameter of seller_performance"


def test_invented_entity_ids_are_dropped():
    accepted, dropped, _ = resolve_filters(
        leg="sql",
        intent="seller_performance",
        proposed={"seller_ids": [REAL_SELLER_ID, "the big seller", "SELLER-1"]},
        vocabulary=VOCABULARY,
    )
    assert accepted == {"seller_ids": [REAL_SELLER_ID]}
    assert {d["value"] for d in dropped} == {"the big seller", "SELLER-1"}


@pytest.mark.parametrize(
    "value",
    [
        "'; DROP TABLE ecommerce.orders; --",
        "SP' OR '1'='1",
        "1) UNION SELECT * FROM pg_shadow --",
    ],
)
def test_injection_shaped_filter_values_are_dropped_not_bound(value):
    """Bind parameters already make these inert. They are still not let through."""
    accepted, dropped, _ = resolve_filters(
        leg="sql",
        intent="seller_performance",
        proposed={"seller_states": [value]},
        vocabulary=VOCABULARY,
    )
    assert accepted == {}
    assert len(dropped) == 1


def test_bad_types_are_dropped_with_a_reason():
    accepted, dropped, _ = resolve_filters(
        leg="sql",
        intent="seller_performance",
        proposed={"min_orders": "lots", "max_avg_review_score": "poor"},
        vocabulary=VOCABULARY,
    )
    assert accepted == {}
    assert {d["reason"] for d in dropped} == {"not an integer", "not a number"}


def test_bad_dates_are_dropped():
    accepted, dropped, _ = resolve_filters(
        leg="sql",
        intent="order_summary",
        proposed={"date_from": "last Tuesday", "date_to": "2018-01-01"},
        vocabulary=VOCABULARY,
    )
    assert accepted == {"date_to": "2018-01-01"}
    assert dropped[0]["reason"] == "not an ISO date"


def test_graph_filters_use_the_graph_spec():
    accepted, dropped, _ = resolve_filters(
        leg="graph",
        intent="seller_ticket_product_paths",
        proposed={"severities": ["critical", "apocalyptic"], "min_orders": 5},
        vocabulary=VOCABULARY,
    )
    assert accepted == {"severities": ["critical"]}
    reasons = {d["value"]: d["reason"] for d in dropped}
    assert "apocalyptic" in reasons
    assert reasons[5] == "not a parameter of seller_ticket_product_paths"


# --------------------------------------------------------------------------------
# Sort keys
# --------------------------------------------------------------------------------


def test_sort_key_must_be_on_the_template_allowlist():
    dropped: list[dict] = []
    assert (
        resolve_sort_by("seller_performance", "total_item_revenue", dropped)
        == "total_item_revenue"
    )
    assert dropped == []


def test_unknown_sort_key_falls_back_to_the_default_and_is_recorded():
    dropped: list[dict] = []
    result = resolve_sort_by("seller_performance", "; DROP TABLE x", dropped)
    assert result == SQL_DEFAULT_SORT["seller_performance"]
    assert dropped[0]["key"] == "sort_by"


def test_sort_key_from_another_template_is_rejected():
    dropped: list[dict] = []
    assert (
        resolve_sort_by("seller_performance", "total_product_revenue", dropped)
        == SQL_DEFAULT_SORT["seller_performance"]
    )
    assert dropped


def test_every_template_default_sort_is_one_of_its_own_options():
    for intent, default in SQL_DEFAULT_SORT.items():
        assert default in SQL_SORT_OPTIONS[intent], intent


def test_seller_performance_no_longer_defaults_to_a_volume_ranking():
    """The audit's sharpest consequence of F-03.

    `ORDER BY late_delivery_orders DESC` is an absolute count, so it returned the
    highest-volume sellers -- whose review scores are above the dataset mean -- as
    the evidence for "which sellers have negative complaints".
    """
    assert SQL_DEFAULT_SORT["seller_performance"] == "late_delivery_rate"


# --------------------------------------------------------------------------------
# Binding
# --------------------------------------------------------------------------------


def test_every_declared_parameter_is_bound_even_when_absent():
    """Static templates name every parameter, so every one must be bound each call."""
    for intent, spec in SQL_PARAMETER_SPECS.items():
        parameters = build_bind_parameters("sql", intent, {})
        assert set(parameters) == set(spec), intent

    for intent, spec in GRAPH_PARAMETER_SPECS.items():
        parameters = build_bind_parameters("graph", intent, {})
        assert set(parameters) == set(spec), intent


def test_unsupplied_filters_bind_null_and_declared_defaults_survive():
    parameters = build_bind_parameters("sql", "seller_performance", {})
    assert parameters["seller_ids"] is None
    assert parameters["min_orders"] == 5


def test_supplied_filters_override_defaults():
    parameters = build_bind_parameters("sql", "seller_performance", {"min_orders": 20})
    assert parameters["min_orders"] == 20
