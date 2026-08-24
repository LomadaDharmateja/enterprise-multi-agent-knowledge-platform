"""Generate the synthetic enterprise corpus, deterministically.

Design and rationale: docs/CORPUS_DESIGN.md. Read Part 2 before changing anything
here -- most of what looks arbitrary in this file (the sublinear volume term, the
latent trait, the per-record RNG) is load-bearing and is there to satisfy a
measured criterion.

Two properties this module must keep:

1. **Bit-identical regeneration.** Two runs from a clean clone against the same
   database produce byte-identical files. Every ORDER BY is total, every RNG is
   seeded from the entity it describes rather than from a shared positional
   stream, and no wall-clock time is read. docs/CORPUS_DESIGN.md Part 1
   enumerates the nine ways the previous version failed this.

2. **The corpus carries information the structured tables do not.** Complaint
   volume correlates with seller quality in the range 0.3-0.6 -- high enough to
   be real signal, low enough that a measurable part of it is not recoverable
   from any PostgreSQL column. Allocating complaints purely from SQL-computable
   quality gives rho ~ 0.9, which is as tautological as v1's volume-driven 0.87.
   See docs/CORPUS_DESIGN.md Part 2 section 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from random import Random
from typing import Any

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine


# --- committed generation constants ------------------------------------------
# These are part of the corpus's identity. Changing any of them changes every
# artifact ID, so they are committed rather than passed in.

CORPUS_SEED = "enterprise-ai-m1-v1"

#: Weight on the SQL-observable quality deficit; 1 - W goes to the latent
#: operational trait. This is the dial that sets the headline correlation.
#: W=0.45 measured rho(complaints, late_rate) = +0.425. See Part 2 section 4.
DEFICIT_WEIGHT = 0.45

#: Concentration of complaints on the worse tail.
INTENSITY_EXPONENT = 3.0

#: Sublinear volume term. This is what holds rho(complaints, n_orders) at ~0.37
#: instead of v1's +0.867 -- a seller with 100x the orders gets more complaints,
#: but not 100x more.
VOLUME_EXPONENT = 0.6

#: all-MiniLM-L6-v2's context window. Every document_text must fit with margin;
#: see docs/CORPUS_DESIGN.md Part 3 D-6 for why this is asserted here.
MAX_DOCUMENT_WORDPIECES = 256
WORDPIECE_SAFETY_MARGIN = 0.85


SEVERITIES = ["low", "medium", "high", "critical"]

#: Cut points for derive_severity, taken from the p15/p50/p85 of the measured
#: score distribution so the four levels land at roughly 15/35/35/15 percent.
SEVERITY_MEDIUM_THRESHOLD = 2.03
SEVERITY_HIGH_THRESHOLD = 3.30
SEVERITY_CRITICAL_THRESHOLD = 4.39

#: Cut points for derive_resolution, from the p65/p80 of the measured pressure
#: distribution. A first guess of 0.55 left 71% of tickets unresolved, which
#: made the email subset a near-clone of the ticket set again.
RESOLUTION_MIXED_THRESHOLD = 0.590
RESOLUTION_UNRESOLVED_THRESHOLD = 0.645

SENTIMENTS = ["angry", "frustrated", "resigned", "neutral", "satisfied_after_resolution"]

RESOLUTIONS = [
    "refund_issued",
    "replacement_shipped",
    "partial_refund",
    "explained_no_action",
    "unresolved",
    "withdrawn_by_customer",
]

ESCALATION_TIERS = ["none", "tier_2", "ops_review", "seller_account_review"]

ISSUE_TYPES = [
    "delayed_delivery",
    "damaged_item",
    "product_quality_complaint",
    "missing_item",
    "payment_question",
    "negative_review_escalation",
]

SUPPORT_CHANNELS = ["email", "chat", "phone", "self_service_portal"]

#: Root causes a ticket can have. None of this is derivable from Olist columns --
#: it is the independent information the vector layer exists to carry.
ROOT_CAUSES = {
    "delayed_delivery": [
        "carrier hub backlog in the destination state",
        "seller dispatched after the shipping limit date",
        "address incomplete, redelivery required",
        "inter-state transfer missed the weekly line haul",
    ],
    "damaged_item": [
        "insufficient inner packaging for a fragile item",
        "carton crushed in transit stacking",
        "product returned damaged after a failed first delivery",
    ],
    "product_quality_complaint": [
        "unit differs from the listing photograph",
        "material quality below the described grade",
        "product failed within the first week of use",
    ],
    "missing_item": [
        "order split across two shipments, second never sent",
        "picking error at the seller warehouse",
        "accessory in the description not in the box",
    ],
    "payment_question": [
        "instalment plan applied differently from checkout display",
        "voucher not deducted from the final charge",
        "duplicate authorisation held on the card",
    ],
    "negative_review_escalation": [
        "customer escalated after an unanswered review comment",
        "repeat complaint about the same product from one customer",
        "public review contradicts the ticket resolution",
    ],
}

LOGISTICS_ROOT_CAUSES = [
    ("carrier_backlog", "carrier", "carrier sorting hub exceeded capacity for the region"),
    ("weather_disruption", "force_majeure", "regional flooding closed the primary route"),
    ("customs_hold", "force_majeure", "inter-state fiscal document held the shipment"),
    ("address_error", "customer", "delivery address incomplete on the order record"),
    ("hub_missort", "carrier", "parcel mis-sorted to the wrong distribution centre"),
    ("seller_dispatch_delay", "seller", "seller handed the parcel over after the shipping limit"),
]

DEFECT_MODES = [
    "component_failure_within_warranty",
    "cosmetic_defect_on_arrival",
    "intermittent_electrical_fault",
    "seam_or_joint_separation",
    "missing_or_incorrect_part",
    "premature_wear",
]

CLAIM_OUTCOMES = ["approved", "rejected", "pending", "withdrawn"]

POLICY_TOPICS = [
    ("late_delivery_compensation", "Late Delivery Compensation"),
    ("damaged_goods_returns", "Damaged Goods and Returns"),
    ("warranty_claim_handling", "Warranty Claim Handling"),
    ("payment_dispute_resolution", "Payment Dispute Resolution"),
    ("seller_performance_review", "Seller Performance Review"),
    ("escalation_and_ombudsman", "Escalation and Ombudsman Referral"),
    ("fragile_goods_packaging", "Fragile Goods Packaging Standards"),
    ("repeat_complaint_handling", "Repeat Complaint Handling"),
]


# --- deterministic primitives -------------------------------------------------


def stable_hash_unit(*parts: Any) -> float:
    """A stable float in [0, 1) from the committed seed and the given parts.

    Pure function of committed inputs, so it survives a clean clone. Used for the
    per-seller operational trait, which must be reproducible but must NOT be
    derivable from any Olist column.
    """
    payload = "|".join([CORPUS_SEED, *(str(part) for part in parts)])
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def record_rng(*parts: Any) -> Random:
    """An RNG seeded from the entity a record describes.

    docs/CORPUS_DESIGN.md Part 1 N-4: the previous version drew every field from
    one global stream consumed in dataframe order, so each draw was bound to a
    *position* rather than to a record. Two consequences, both fixed here: a
    reordering silently changed record content, and a branch consuming a
    different number of draws desynchronised every record after it. Since M1
    requires severity to vary, the second one would have gone from corrupting 2
    records to corrupting the entire tail of the file.

    With a per-record RNG a record's fields depend only on its own identity.
    Reordering cannot change content and branch divergence cannot cascade.
    """
    payload = "|".join([CORPUS_SEED, *(str(part) for part in parts)])
    seed = int.from_bytes(hashlib.sha256(payload.encode("utf-8")).digest()[:8], "big")
    return Random(seed)


def pct_rank(series: pd.Series) -> pd.Series:
    """Percentile rank in [0, 1]. Robust to the heavy skew in n_orders."""
    return series.rank(pct=True, method="average")


def assert_unique_key(df: pd.DataFrame, columns: list[str], label: str) -> None:
    """Guard against docs/CORPUS_DESIGN.md Part 1 N-1/N-2/N-5.

    Every selection this module makes must be resolvable to a total order. This
    turns 'the ORDER BY looked unique' into a check.
    """
    duplicated = int(df.duplicated(subset=columns, keep=False).sum())
    if duplicated:
        raise ValueError(
            f"{label}: {duplicated} rows are tied on {columns}, so the ordering is "
            f"not total and generation would not be reproducible. Add a unique "
            f"tiebreaker to the ORDER BY."
        )


# --- serialisation ------------------------------------------------------------


def sanitize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if isinstance(value, (dict, list)):
        return value
    if pd.isna(value):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date, pd.Timestamp)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    return value


def sanitize_record(record: dict[str, Any]) -> dict[str, Any]:
    return {key: sanitize_value(value) for key, value in record.items()}


#: Entity IDs promoted to the top level of every record.
#: qdrant_ingest.build_payload() reads these keys from the TOP level of a record
#: while the generator's canonical copy lives under linked_entities -- AUDIT.md
#: F-01, which is why 0 of 8,152 Qdrant points could join back to SQL or graph
#: evidence. Fixing ingest to read linked_entities is M2 scope; writing the IDs
#: where the existing consumer already looks is the producer's half, and M1's
#: exit criterion needs it because the boundary test reads seller_id from Qdrant
#: metadata rather than from this file.
PROMOTED_ENTITY_KEYS = [
    "customer_id",
    "customer_unique_id",
    "order_id",
    "product_id",
    "seller_id",
    "review_id",
    "category_id",
    "region_id",
]


def promote_entity_ids(record: dict[str, Any]) -> dict[str, Any]:
    linked = record.get("linked_entities") or {}
    for key in PROMOTED_ENTITY_KEYS:
        value = linked.get(key)
        if value is not None and key not in record:
            record[key] = value
    return record


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as file:
        for record in records:
            # sort_keys: Part 1 N-9. Insertion order is stable today; this makes
            # it stable by construction rather than by luck.
            file.write(json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n")


def build_postgres_engine() -> Engine:
    load_dotenv()
    user = os.getenv("POSTGRES_USER", "enterprise_user")
    password = os.getenv("POSTGRES_PASSWORD", "enterprise_password")
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = os.getenv("POSTGRES_PORT", "5432")
    database = os.getenv("POSTGRES_DB", "enterprise_ai")
    return create_engine(f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}")


def read_dataframe(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> pd.DataFrame:
    return pd.read_sql(text(sql), engine, params=params or {})


def generate_id(prefix: str, index: int) -> str:
    return f"{prefix}-{index:06d}"


def normalize_text(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, float) and pd.isna(value):
        return fallback
    text_value = str(value).strip()
    return text_value or fallback


def derive_created_at(
    rng: Random,
    base_value: Any,
    offset_days_min: int = 1,
    offset_days_max: int = 12,
) -> str:
    """Timestamp derived from the order's own dates plus a per-record offset.

    Part 1 N-3 and N-8: no datetime.now() anywhere, and no wall-clock fallback.
    A record with no usable timestamp is an error, not a silent guess.
    """
    if base_value is None or pd.isna(base_value):
        raise ValueError(
            "derive_created_at received no usable base timestamp. The previous "
            "version fell back to datetime.now(), which is unreproducible and "
            "silent (docs/CORPUS_DESIGN.md Part 1 N-8)."
        )
    base_dt = pd.to_datetime(base_value).to_pydatetime()
    created = base_dt + timedelta(
        days=rng.randint(offset_days_min, offset_days_max),
        hours=rng.randint(0, 23),
        minutes=rng.randint(0, 59),
    )
    return created.isoformat()


# --- SQL ----------------------------------------------------------------------

SELLER_ATTRIBUTES_SQL = """
WITH seller_orders AS (
    SELECT DISTINCT oi.seller_id, oi.order_id
    FROM ecommerce.order_items oi
    WHERE oi.seller_id IS NOT NULL
),
seller_reviews AS (
    SELECT so.seller_id,
           count(r.review_id)              AS n_reviews,
           avg(r.review_score)::float      AS mean_review,
           sum((r.review_score <= 2)::int) AS n_bad_reviews
    FROM seller_orders so
    JOIN ecommerce.reviews r ON r.order_id = so.order_id
    GROUP BY 1
),
seller_delivery AS (
    SELECT so.seller_id,
           count(*)                                      AS n_orders,
           sum((os.is_late_delivery IS TRUE)::int)        AS n_late,
           avg((os.is_late_delivery IS TRUE)::int)::float AS late_rate,
           avg(os.delivery_delay_days)::float             AS mean_delay,
           sum(os.total_payment_value)::float             AS revenue
    FROM seller_orders so
    JOIN ecommerce.vw_order_summary os ON os.order_id = so.order_id
    GROUP BY 1
),
seller_category AS (
    SELECT DISTINCT ON (t.seller_id)
        t.seller_id, t.product_category_name, t.category_share
    FROM (
        SELECT oi.seller_id,
               p.product_category_name,
               count(*)                                                            AS items,
               count(*)::float / sum(count(*)) OVER (PARTITION BY oi.seller_id)     AS category_share
        FROM ecommerce.order_items oi
        JOIN ecommerce.products p ON oi.product_id = p.product_id
        WHERE p.product_category_name IS NOT NULL AND oi.seller_id IS NOT NULL
        GROUP BY 1, 2
    ) t
    -- total order: items desc, then category name. Part 1 N-5.
    ORDER BY t.seller_id, t.items DESC, t.product_category_name
)
SELECT s.seller_id,
       s.seller_city,
       s.seller_state,
       d.n_orders, d.n_late, d.late_rate, d.mean_delay, d.revenue,
       r.n_reviews, r.mean_review, r.n_bad_reviews,
       c.product_category_name AS dominant_category,
       c.category_share,
       pct.product_category_name_english AS dominant_category_english
FROM ecommerce.sellers s
LEFT JOIN seller_delivery d ON d.seller_id = s.seller_id
LEFT JOIN seller_reviews  r ON r.seller_id = s.seller_id
LEFT JOIN seller_category c ON c.seller_id = s.seller_id
LEFT JOIN ecommerce.product_category_translations pct
       ON pct.product_category_name = c.product_category_name
ORDER BY s.seller_id;
"""


#: One row per (seller, order). Every tiebreak is total: the DISTINCT ON keys end
#: in a unique column (Part 1 N-5), and the outer ORDER BY ends in order_id.
SELLER_ORDERS_SQL = """
WITH first_item AS (
    SELECT DISTINCT ON (oi.order_id)
        oi.order_id, oi.product_id, oi.seller_id,
        p.product_category_name,
        pct.product_category_name_english
    FROM ecommerce.order_items oi
    JOIN ecommerce.products p ON oi.product_id = p.product_id
    LEFT JOIN ecommerce.product_category_translations pct
        ON p.product_category_name = pct.product_category_name
    WHERE oi.product_id IS NOT NULL
      AND oi.seller_id IS NOT NULL
      AND p.product_category_name IS NOT NULL
    ORDER BY oi.order_id, oi.price DESC NULLS LAST, oi.order_item_id
),
lowest_review AS (
    SELECT DISTINCT ON (r.order_id)
        r.order_id, r.review_id, r.review_score, r.sentiment_label,
        r.review_comment_message
    FROM ecommerce.reviews r
    ORDER BY r.order_id, r.review_score ASC NULLS LAST,
             r.review_creation_date DESC NULLS LAST, r.review_id
)
SELECT
    os.order_id, os.customer_id, os.customer_unique_id,
    os.customer_city, os.customer_state,
    os.order_status, os.delivery_status,
    os.order_purchase_timestamp, os.order_delivered_customer_date,
    os.order_estimated_delivery_date,
    os.is_late_delivery, os.delivery_delay_days, os.total_payment_value,
    os.payment_types,
    fi.product_id, fi.seller_id,
    fi.product_category_name, fi.product_category_name_english,
    lr.review_id, lr.review_score, lr.sentiment_label, lr.review_comment_message
FROM ecommerce.vw_order_summary os
JOIN first_item fi ON os.order_id = fi.order_id
LEFT JOIN lowest_review lr ON os.order_id = lr.order_id
WHERE os.order_purchase_timestamp IS NOT NULL
ORDER BY os.order_id;
"""


CATEGORIES_SQL = """
SELECT product_category_name, product_category_name_english
FROM ecommerce.product_category_translations
ORDER BY product_category_name;
"""


def fetch_seller_attributes(engine: Engine) -> pd.DataFrame:
    df = read_dataframe(engine, SELLER_ATTRIBUTES_SQL)
    assert_unique_key(df, ["seller_id"], "seller attributes")
    return df


def fetch_seller_orders(engine: Engine) -> pd.DataFrame:
    df = read_dataframe(engine, SELLER_ORDERS_SQL)
    assert_unique_key(df, ["order_id"], "seller orders")
    return df


def fetch_categories(engine: Engine) -> pd.DataFrame:
    df = read_dataframe(engine, CATEGORIES_SQL)
    assert_unique_key(df, ["product_category_name"], "categories")
    missing = df["product_category_name_english"].isna().sum()
    if missing:
        raise ValueError(
            f"{missing} categories have no English name. AUDIT.md F-04 -- policy "
            f"documents are titled from this column and would read 'None'. Fix "
            f"postgres_loader.MANUAL_CATEGORY_TRANSLATIONS before generating."
        )
    return df


# --- seller scoring and complaint allocation ----------------------------------


def score_sellers(seller_attrs: pd.DataFrame) -> pd.DataFrame:
    """Quality deficit from Olist columns, plus the latent operational trait.

    docs/CORPUS_DESIGN.md Part 2 sections 2-3. The deficit alone yields
    rho ~ 0.9 against complaint count, which means PostgreSQL recovers the corpus
    exactly and the vector layer carries nothing new. The latent trait -- how a
    seller actually behaves once something goes wrong, which Olist does not
    record -- is what moves the correlation into the 0.3-0.6 band where a
    measurable part of the signal is genuinely unrecoverable from SQL.
    """
    df = seller_attrs.copy()
    scored = df[df["n_orders"].notna() & df["mean_review"].notna()].copy()

    scored["bad_share"] = scored["n_bad_reviews"] / scored["n_reviews"]
    raw_deficit = (
        0.40 * pct_rank(scored["late_rate"])
        + 0.40 * pct_rank(-scored["mean_review"])
        + 0.20 * pct_rank(scored["bad_share"])
    )
    scored["quality_deficit"] = pct_rank(raw_deficit)
    scored["latent_ops_trait"] = [
        stable_hash_unit("ops", seller_id) for seller_id in scored["seller_id"]
    ]
    scored["complaint_intensity"] = pct_rank(
        DEFICIT_WEIGHT * scored["quality_deficit"]
        + (1.0 - DEFICIT_WEIGHT) * scored["latent_ops_trait"]
    )
    return scored


def allocate_complaints(
    scored: pd.DataFrame, total: int, available: pd.Series | None = None
) -> pd.Series:
    """Complaints per seller: intensity^alpha * log1p(n_orders)^beta, capped.

    The log1p term is deliberate and load-bearing. Complaint count should rise
    with order volume -- it does in reality -- but v1 let volume dominate
    (rho = +0.867 against n_orders), so "which sellers have the most complaints"
    reduced to "which sellers have the most orders". Sublinear keeps volume
    present and subordinate.
    """
    weight = (scored["complaint_intensity"] ** INTENSITY_EXPONENT) * (
        np.log1p(scored["n_orders"]) ** VOLUME_EXPONENT
    )
    raw = total * weight / weight.sum()
    # Cap on orders that can ACTUALLY be selected, not on the seller's order
    # count from the attribute query. The two differ: seller_orders requires a
    # first item with a non-null category and a purchase timestamp, so a seller
    # with n_orders=5 may offer only 3 selectable rows. Capping on the wrong
    # number silently under-delivered 7 of 3,000 tickets.
    if available is None:
        cap = scored["n_orders"].astype(int)
    else:
        cap = scored["seller_id"].map(available).fillna(0).astype(int)
    counts = np.minimum(np.floor(raw).astype(int), cap)

    shortfall = total - int(counts.sum())
    if shortfall > 0:
        remainder = raw - np.floor(raw)
        has_room = (cap - counts) > 0
        order = (
            pd.DataFrame({"remainder": remainder, "seller_id": scored["seller_id"]})[has_room]
            .sort_values(["remainder", "seller_id"], ascending=[False, True])
            .index[:shortfall]
        )
        counts.loc[order] += 1

    return counts


def select_complaint_orders(
    scored: pd.DataFrame,
    complaint_counts: pd.Series,
    seller_orders: pd.DataFrame,
) -> pd.DataFrame:
    """Pick which of a seller's orders become complaints.

    Within a seller, rank by how bad the order actually was (worst review first,
    longest delay next), tie broken by order_id so the ordering is total.
    """
    counts = dict(zip(scored["seller_id"], complaint_counts))
    wanted = {seller: int(n) for seller, n in counts.items() if n > 0}

    candidates = seller_orders[seller_orders["seller_id"].isin(wanted)].copy()
    candidates["_review_rank"] = candidates["review_score"].fillna(3.0)
    candidates["_delay_rank"] = -candidates["delivery_delay_days"].fillna(0.0)
    candidates = candidates.sort_values(
        ["seller_id", "_review_rank", "_delay_rank", "order_id"],
        ascending=[True, True, True, True],
        kind="mergesort",
    )
    candidates["_within"] = candidates.groupby("seller_id", sort=True).cumcount()
    limits = candidates["seller_id"].map(wanted)
    selected = candidates[candidates["_within"] < limits].copy()

    return selected.sort_values("order_id", kind="mergesort").reset_index(drop=True)


# --- linked entities ----------------------------------------------------------


def build_linked_entities(row: pd.Series) -> dict[str, Any]:
    state = row.get("customer_state")
    city = row.get("customer_city")
    has_region = state is not None and city is not None and not pd.isna(state) and not pd.isna(city)
    return sanitize_record(
        {
            "customer_id": row.get("customer_id"),
            "customer_unique_id": row.get("customer_unique_id"),
            "order_id": row.get("order_id"),
            "product_id": row.get("product_id"),
            "seller_id": row.get("seller_id"),
            "review_id": row.get("review_id"),
            "category_id": row.get("product_category_name"),
            "category_name_english": row.get("product_category_name_english"),
            "customer_state": state,
            "customer_city": city,
            "region_id": f"{state}::{city}" if has_region else None,
        }
    )


# --- support tickets ----------------------------------------------------------


def derive_issue_type(rng: Random, row: pd.Series) -> str:
    """Issue type from the order's own facts, with the RNG breaking only ties.

    Unlike v1 this always consumes exactly one draw, but that is now a
    convenience rather than a correctness requirement -- the per-record RNG means
    a branch consuming a different number of draws cannot desynchronise anything.
    """
    review_score = row.get("review_score")
    is_late = row.get("is_late_delivery") is True
    delay = row.get("delivery_delay_days")
    payment_types = normalize_text(row.get("payment_types")).lower()

    pool: list[str]
    if is_late or (pd.notna(delay) and float(delay) > 3):
        pool = ["delayed_delivery", "delayed_delivery", "missing_item"]
    elif pd.notna(review_score) and float(review_score) <= 2:
        pool = [
            "product_quality_complaint",
            "damaged_item",
            "negative_review_escalation",
            "missing_item",
        ]
    elif "voucher" in payment_types or "boleto" in payment_types:
        pool = ["payment_question", "payment_question", "product_quality_complaint"]
    else:
        pool = ISSUE_TYPES
    return rng.choice(pool)


def derive_severity(rng: Random, row: pd.Series, issue_type: str, latent: float) -> str:
    """Severity varies -- the single most important difference from v1.

    v1 emitted severity='high' for all 3,000 tickets, because every selected order
    had review_score=1 and the score<=1 branch returned a literal. Here severity
    is a score over delay magnitude, review score, issue type and the seller's
    operational trait, so all four levels occur.
    """
    score = 0.0
    delay = row.get("delivery_delay_days")
    if pd.notna(delay):
        delay_days = float(delay)
        if delay_days >= 20:
            score += 2.0
        elif delay_days >= 10:
            score += 1.4
        elif delay_days >= 5:
            score += 0.8
        elif delay_days > 0:
            score += 0.3

    review_score = row.get("review_score")
    if pd.notna(review_score):
        score += {1: 1.6, 2: 1.1, 3: 0.6, 4: 0.2, 5: 0.0}[int(review_score)]

    if issue_type in {"damaged_item", "missing_item", "negative_review_escalation"}:
        score += 0.6
    elif issue_type == "payment_question":
        score -= 0.2

    # The seller's operational trait shifts severity: a seller who handles
    # problems badly accumulates worse ones. Not visible in any Olist column.
    score += 1.2 * latent
    score += 0.5 * rng.random()

    # Thresholds are the measured p85/p50/p15 of the score distribution over the
    # selected complaint orders, not round numbers. Set by guess they produced
    # 55% critical, which is variance without being realistic variance --
    # "critical" has to be the rare tail or the field carries no ranking signal.
    if score >= SEVERITY_CRITICAL_THRESHOLD:
        return "critical"
    if score >= SEVERITY_HIGH_THRESHOLD:
        return "high"
    if score >= SEVERITY_MEDIUM_THRESHOLD:
        return "medium"
    return "low"


def derive_sentiment(rng: Random, severity: str, resolution: str, contact_count: int) -> str:
    """Sentiment is deliberately NOT collinear with review score.

    A customer can be satisfied after a good recovery on a critical issue, or
    still angry about a medium one that nobody answered. That decoupling is part
    of what the structured tables cannot express.
    """
    if resolution in {"refund_issued", "replacement_shipped"} and contact_count <= 2:
        pool = ["satisfied_after_resolution", "neutral", "resigned"]
    elif resolution == "unresolved":
        pool = ["angry", "frustrated", "frustrated", "resigned"]
    elif severity in {"critical", "high"}:
        pool = ["angry", "frustrated", "resigned"]
    else:
        pool = ["frustrated", "neutral", "resigned", "satisfied_after_resolution"]
    return rng.choice(pool)


def derive_resolution(rng: Random, severity: str, latent: float) -> tuple[str, str, int, int | None]:
    """Resolution, escalation tier, contact count and days to resolution.

    None of these four exist in the structured schema. `latent` is the seller's
    operational trait, so a seller who does not answer produces unresolved
    tickets regardless of how good their review average looks.
    """
    unresolved_pressure = 0.55 * latent + 0.25 * rng.random()
    if severity == "critical":
        unresolved_pressure += 0.12

    if unresolved_pressure >= RESOLUTION_UNRESOLVED_THRESHOLD:
        resolution = "unresolved"
    elif unresolved_pressure >= RESOLUTION_MIXED_THRESHOLD:
        resolution = rng.choice(["partial_refund", "explained_no_action", "unresolved"])
    elif severity in {"critical", "high"}:
        resolution = rng.choice(["refund_issued", "replacement_shipped", "partial_refund"])
    else:
        resolution = rng.choice(
            ["explained_no_action", "refund_issued", "replacement_shipped", "withdrawn_by_customer"]
        )

    if resolution == "unresolved" and severity in {"critical", "high"}:
        escalation = rng.choice(["ops_review", "seller_account_review", "tier_2"])
    elif severity == "critical":
        escalation = rng.choice(["tier_2", "ops_review"])
    elif resolution == "unresolved":
        escalation = rng.choice(["none", "tier_2"])
    else:
        escalation = rng.choice(["none", "none", "tier_2"])

    contact_count = 1 + int(rng.random() * (4 if resolution == "unresolved" else 2))
    days_to_resolution = None if resolution == "unresolved" else 1 + int(rng.random() * 21)
    return resolution, escalation, contact_count, days_to_resolution


def ticket_customer_message(rng: Random, issue_type: str, severity: str, contact_count: int) -> str:
    """Generated English.

    docs/CORPUS_DESIGN.md Part 3: v1 put verbatim Portuguese Olist review text
    here and embedded it with an English-only MiniLM. The original text is kept
    in `source_review_excerpt`, which is not embedded, so provenance survives.
    """
    openers = {
        "delayed_delivery": "My order has not arrived",
        "damaged_item": "The item arrived damaged",
        "product_quality_complaint": "The product does not match the listing",
        "missing_item": "Part of my order is missing",
        "payment_question": "I have a question about my charge",
        "negative_review_escalation": "My review went unanswered",
    }
    urgency = {
        "critical": "This is urgent.",
        "high": "Please resolve quickly.",
        "medium": "Please let me know what happens next.",
        "low": "No rush, but please take a look.",
    }
    # No follow-up sentence: document_text already carries "{n} contacts" as a
    # structured field, so restating it in the prose spent word-pieces on a fact
    # the record already had. It was the longest documents that paid for it.
    follow_up = ""
    detail = rng.choice(
        [
            "Tracking has not updated.",
            "Photographs attached.",
            "I was told this would be handled.",
            "The delivery estimate has passed.",
            "I would prefer a replacement.",
        ]
    )
    return f"{openers[issue_type]}. {detail}{follow_up} {urgency[severity]}"


def agent_note(resolution: str, escalation: str) -> str:
    """Deliberately terse, and deliberately does not restate root_cause.

    root_cause is already its own field and already in document_text; repeating
    it here cost 18 word-pieces of a budget the entity IDs consume 117 of. The
    per-record RNG argument is gone with the filler sentence it used to pick.
    """
    action = {
        "refund_issued": "Refund processed to the original payment method.",
        "replacement_shipped": "Replacement dispatched with new tracking.",
        "partial_refund": "Partial refund agreed as a goodwill gesture.",
        "explained_no_action": "Policy position explained; no compensation due.",
        "unresolved": "Awaiting seller response; customer informed.",
        "withdrawn_by_customer": "Customer withdrew after the item arrived.",
    }[resolution]
    escalation_note = {
        "none": "Handled at first line.",
        "tier_2": "Escalated to tier 2.",
        "ops_review": "Referred to operations for a carrier review.",
        "seller_account_review": "Flagged to the seller account team.",
    }[escalation]
    return f"{action} {escalation_note}"


def build_ticket_document_text(record: dict[str, Any]) -> str:
    """Compact by necessity, not by preference.

    The four entity IDs cost ~117 of the ~217 word-piece budget on their own and
    they are not negotiable -- F-01's whole point is that a vector hit must carry
    the IDs that let it join back to SQL and graph evidence. So the connective
    prose goes instead: labelled fields, no sentences built around them.
    """
    linked = record["linked_entities"]
    return (
        f"Support Ticket {record['ticket_id']}. "
        f"Order {linked.get('order_id')}. Customer {linked.get('customer_id')}. "
        f"Product {linked.get('product_id')}. Seller {linked.get('seller_id')}. "
        f"Category {linked.get('category_name_english') or linked.get('category_id')}. "
        f"Issue {record['issue_type']}, severity {record['severity']}, "
        f"sentiment {record['customer_sentiment']}. "
        f"Cause: {record['root_cause']}. "
        f"Resolution {record['resolution']}, escalation {record['escalation_tier']}, "
        f"{record['contact_count']} contacts. "
        f"Customer: {record['customer_message']} Agent: {record['agent_notes']}"
    )


def generate_support_tickets(
    selected_orders: pd.DataFrame,
    latent_by_seller: dict[str, float],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    for index, (_, row) in enumerate(selected_orders.iterrows(), start=1):
        order_id = row["order_id"]
        rng = record_rng("support_ticket", order_id)
        latent = latent_by_seller.get(row["seller_id"], 0.5)

        issue_type = derive_issue_type(rng, row)
        severity = derive_severity(rng, row, issue_type, latent)
        resolution, escalation, contact_count, days_to_resolution = derive_resolution(
            rng, severity, latent
        )
        sentiment = derive_sentiment(rng, severity, resolution, contact_count)
        root_cause = rng.choice(ROOT_CAUSES[issue_type])

        record: dict[str, Any] = {
            "ticket_id": generate_id("TCK", index),
            "artifact_type": "support_tickets",
            "issue_type": issue_type,
            "severity": severity,
            "customer_sentiment": sentiment,
            "root_cause": root_cause,
            "resolution": resolution,
            "escalation_tier": escalation,
            "contact_count": contact_count,
            "days_to_resolution": days_to_resolution,
            "status": "closed" if resolution != "unresolved" else "open",
            "channel": rng.choice(SUPPORT_CHANNELS),
            "title": (
                f"{issue_type.replace('_', ' ').title()} for "
                f"{normalize_text(row.get('product_category_name_english'), 'general merchandise')}"
            ),
            "created_at": derive_created_at(
                rng,
                row.get("order_delivered_customer_date")
                if pd.notna(row.get("order_delivered_customer_date"))
                else row.get("order_purchase_timestamp"),
            ),
            "customer_message": ticket_customer_message(rng, issue_type, severity, contact_count),
            "agent_notes": agent_note(resolution, escalation),
            # Kept for provenance, deliberately NOT part of document_text: it is
            # Portuguese and the embedding model is English-only.
            "source_review_excerpt": normalize_text(row.get("review_comment_message")) or None,
            "linked_entities": build_linked_entities(row),
        }
        record["summary"] = (
            f"{severity} severity {issue_type.replace('_', ' ')} on order {order_id}, "
            f"resolution {resolution.replace('_', ' ')}"
        )
        record["document_text"] = build_ticket_document_text(record)
        records.append(sanitize_record(record))

    return records


# --- customer emails ----------------------------------------------------------


def generate_customer_emails(support_tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Follow-up threads on a biased subset of tickets, not a 1:1 clone.

    v1 emitted one email per ticket, reusing `linked_entities` by reference, so
    the file carried no entity information the ticket file did not already have
    (AUDIT.md: 8,152 artifacts, 3,000 + 1,000 rows of entity content). Here a
    thread exists only where the case actually dragged, which makes "which
    sellers generate follow-up threads" a different question from "which sellers
    have tickets" -- and neither is answerable in SQL.
    """
    # Tightened after measurement: the first rule matched 2,745 of 2,993
    # tickets, which recreated exactly the v1 problem this split exists to fix.
    # A follow-up thread should mean the case actually dragged.
    eligible = [
        ticket
        for ticket in support_tickets
        if ticket["resolution"] == "unresolved"
        or ticket["escalation_tier"] in {"ops_review", "seller_account_review"}
        or ticket["contact_count"] >= 4
    ]

    records: list[dict[str, Any]] = []
    for index, ticket in enumerate(eligible, start=1):
        linked = ticket["linked_entities"]
        rng = record_rng("customer_email", ticket["ticket_id"])
        message_count = 1 + int(rng.random() * 3)

        tone = {
            "angry": "I am extremely unhappy with how this has been handled.",
            "frustrated": "This has taken far longer than I expected.",
            "resigned": "I am not sure there is much more I can do here.",
            "neutral": "I would like an update when you have one.",
            "satisfied_after_resolution": "Thank you for sorting this out.",
        }[ticket["customer_sentiment"]]

        body_parts = [
            f"Hello Support Team,",
            f"I am following up on ticket {ticket['ticket_id']} for order "
            f"{linked.get('order_id')}.",
            tone,
        ]
        if ticket["resolution"] == "unresolved":
            body_parts.append(
                f"It has now been {ticket['contact_count']} contacts with no resolution."
            )
        else:
            body_parts.append(
                f"The case was closed as {ticket['resolution'].replace('_', ' ')}."
            )
        body_parts.append(rng.choice([
            "Please confirm what happens next.",
            "I would like this escalated if nothing changes this week.",
            "Let me know if you need anything else from me.",
        ]))

        record = {
            "email_id": generate_id("EML", index),
            "artifact_type": "customer_emails",
            "subject": f"Re: {ticket['title']} ({ticket['ticket_id']})",
            "message_count": message_count,
            "thread_status": "awaiting_customer" if ticket["resolution"] != "unresolved" else "awaiting_seller",
            # `ticket_id` (not just related_ticket_id): synthetic_neo4j_loader
            # matches the RELATED_TO_TICKET edge on row.ticket_id.
            "ticket_id": ticket["ticket_id"],
            "related_ticket_id": ticket["ticket_id"],
            "severity": ticket["severity"],
            "customer_sentiment": ticket["customer_sentiment"],
            "created_at": ticket["created_at"],
            "body": " ".join(body_parts),
            "linked_entities": dict(linked),
        }
        record["summary"] = (
            f"Follow-up thread of {message_count} messages on {ticket['ticket_id']}, "
            f"{record['thread_status'].replace('_', ' ')}"
        )
        record["document_text"] = (
            f"Customer Email {record['email_id']} on ticket {ticket['ticket_id']} "
            f"for order {linked.get('order_id')}, customer {linked.get('customer_id')}, "
            f"seller {linked.get('seller_id')}. "
            f"Thread status {record['thread_status']}, {message_count} messages, "
            f"sentiment {ticket['customer_sentiment']}. {record['body']}"
        )
        records.append(sanitize_record(record))

    return records


# --- logistics incidents ------------------------------------------------------


def generate_logistics_incidents(
    seller_orders: pd.DataFrame,
    latent_by_seller: dict[str, float],
    max_records: int,
) -> list[dict[str, Any]]:
    """Late orders, with a root cause and a fault party.

    `fault_party` is the field that most justifies the vector layer. A seller
    with a high late_rate whose incidents are all fault_party='carrier' is being
    punished by SQL for something outside their control, and no structured
    column can say so.
    """
    late = seller_orders[seller_orders["is_late_delivery"] == True].copy()  # noqa: E712
    # Total order: delay desc, then order_id. Part 1 N-1 -- the single missing
    # tiebreaker here re-pointed 277 of 1,000 incidents on every regeneration.
    late = late.sort_values(
        ["delivery_delay_days", "order_id"], ascending=[False, True], kind="mergesort"
    ).head(max_records)
    assert_unique_key(late, ["order_id"], "logistics candidates")

    records: list[dict[str, Any]] = []
    for index, (_, row) in enumerate(late.iterrows(), start=1):
        order_id = row["order_id"]
        rng = record_rng("logistics_incident", order_id)
        latent = latent_by_seller.get(row["seller_id"], 0.5)
        delay_days = float(row["delivery_delay_days"]) if pd.notna(row["delivery_delay_days"]) else 0.0

        # A seller with a poor operational trait is more likely to be at fault;
        # otherwise the carrier and external causes dominate, as in reality.
        if rng.random() < 0.25 + 0.35 * latent:
            candidates = [c for c in LOGISTICS_ROOT_CAUSES if c[1] == "seller"]
        else:
            candidates = [c for c in LOGISTICS_ROOT_CAUSES if c[1] != "seller"]
        incident_type, fault_party, root_cause = rng.choice(candidates)

        if delay_days >= 30:
            severity = "critical"
        elif delay_days >= 14:
            severity = "high"
        elif delay_days >= 7:
            severity = "medium"
        else:
            severity = "low"

        resolved = rng.random() > (0.20 + 0.30 * latent)
        record = {
            "incident_id": generate_id("INC", index),
            "artifact_type": "logistics_incidents",
            "incident_type": incident_type,
            "fault_party": fault_party,
            "root_cause": root_cause,
            "severity": severity,
            "status": "resolved" if resolved else rng.choice(["under_review", "escalated", "open"]),
            "delay_days": round(delay_days, 2),
            "recurrence_flag": stable_hash_unit("recurrence", row["seller_id"]) > 0.75,
            "corrective_action": rng.choice([
                "route re-planned through an alternate distribution centre",
                "carrier performance review opened for the lane",
                "seller asked to bring dispatch forward by one day",
                "address validation added at checkout for the region",
                "no corrective action; one-off external event",
            ]),
            "title": (
                f"{incident_type.replace('_', ' ').title()} affecting "
                f"{normalize_text(row.get('customer_state'), 'an unknown state')}"
            ),
            "created_at": derive_created_at(
                rng,
                row.get("order_estimated_delivery_date")
                if pd.notna(row.get("order_estimated_delivery_date"))
                else row.get("order_purchase_timestamp"),
            ),
            "metadata": {
                "customer_state": sanitize_value(row.get("customer_state")),
                "customer_city": sanitize_value(row.get("customer_city")),
                "delivery_status": sanitize_value(row.get("delivery_status")),
                "estimated_delivery": sanitize_value(row.get("order_estimated_delivery_date")),
            },
            "linked_entities": build_linked_entities(row),
        }
        record["summary"] = (
            f"{severity} {incident_type.replace('_', ' ')} on order {order_id}, "
            f"{delay_days:.1f} days late, fault attributed to {fault_party}"
        )
        record["document_text"] = (
            f"Logistics Incident {record['incident_id']} on order {order_id} "
            f"for customer {row.get('customer_id')} and seller {row.get('seller_id')}. "
            f"Type {incident_type}, severity {severity}, {delay_days:.1f} days late. "
            f"Fault party: {fault_party}. Root cause: {root_cause}. "
            f"Corrective action: {record['corrective_action']}. "
            f"Destination {row.get('customer_city')}, {row.get('customer_state')}. "
            f"Status {record['status']}."
        )
        records.append(sanitize_record(record))

    return records


# --- warranty claims ----------------------------------------------------------


def generate_warranty_claims(
    seller_orders: pd.DataFrame,
    scored_sellers: pd.DataFrame,
    latent_by_seller: dict[str, float],
    max_records: int,
    ticket_id_by_order: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Allocated over (seller, product) by defect propensity, independent of tickets.

    v1 took `eligible_tickets[:1000]` -- a prefix slice, so warranty claims could
    not disagree with tickets by construction. The rebuild plan requires that
    some well-rated sellers carry unresolved warranty issues, which is only
    possible if this allocation is independent of the complaint allocation.
    """
    propensity = seller_orders.copy()
    propensity["_defect_score"] = [
        stable_hash_unit("warranty", row_order_id, row_product_id)
        + 0.45 * latent_by_seller.get(row_seller_id, 0.5)
        for row_order_id, row_product_id, row_seller_id in zip(
            propensity["order_id"], propensity["product_id"], propensity["seller_id"]
        )
    ]
    propensity = propensity.sort_values(
        ["_defect_score", "order_id"], ascending=[False, True], kind="mergesort"
    ).head(max_records)
    assert_unique_key(propensity, ["order_id"], "warranty candidates")

    records: list[dict[str, Any]] = []
    for index, (_, row) in enumerate(propensity.iterrows(), start=1):
        order_id = row["order_id"]
        rng = record_rng("warranty_claim", order_id)
        latent = latent_by_seller.get(row["seller_id"], 0.5)

        defect_mode = rng.choice(DEFECT_MODES)
        under_warranty = rng.random() > 0.25
        if not under_warranty:
            outcome = rng.choice(["rejected", "withdrawn"])
        elif rng.random() < 0.25 + 0.40 * latent:
            outcome = rng.choice(["pending", "rejected"])
        else:
            outcome = "approved"

        severity = "critical" if defect_mode.startswith("component") and outcome != "approved" else (
            "high" if outcome in {"pending", "rejected"} else rng.choice(["medium", "low"])
        )

        record = {
            "claim_id": generate_id("WRN", index),
            "artifact_type": "warranty_claims",
            "defect_mode": defect_mode,
            "claim_status": outcome,
            "under_warranty": under_warranty,
            "repeat_defect": stable_hash_unit("repeat", row["product_id"]) > 0.80,
            "severity": severity,
            "resolution_days": None if outcome == "pending" else 2 + int(rng.random() * 28),
            "requested_resolution": rng.choice(
                ["replacement", "refund", "repair", "seller investigation"]
            ),
            "claim_reason": rng.choice([
                "product stopped working within the warranty period",
                "defect present on arrival and reported immediately",
                "same fault recurred after a previous replacement",
                "item does not perform as described in the listing",
            ]),
            "title": (
                f"Warranty claim for "
                f"{normalize_text(row.get('product_category_name_english'), 'general merchandise')}"
            ),
            "created_at": derive_created_at(
                rng,
                row.get("order_delivered_customer_date")
                if pd.notna(row.get("order_delivered_customer_date"))
                else row.get("order_purchase_timestamp"),
                offset_days_min=5,
                offset_days_max=90,
            ),
            "linked_entities": build_linked_entities(row),
        }
        # Where a ticket happens to exist on the same order, record it. This is
        # an observed overlap, not a coupling: the two allocations stay
        # independent, which is what lets a well-rated seller carry an
        # unresolved warranty issue with no complaint against it.
        overlapping_ticket = (ticket_id_by_order or {}).get(order_id)
        if overlapping_ticket:
            record["ticket_id"] = overlapping_ticket
        record["summary"] = (
            f"{severity} warranty claim, {defect_mode.replace('_', ' ')}, "
            f"outcome {outcome}"
        )
        record["document_text"] = (
            f"Warranty Claim {record['claim_id']} on order {order_id} for product "
            f"{row.get('product_id')} from seller {row.get('seller_id')}, "
            f"customer {row.get('customer_id')}. "
            f"Defect mode {defect_mode}, severity {severity}. "
            f"Under warranty: {under_warranty}. Claim status {outcome}. "
            f"Reason: {record['claim_reason']}. "
            f"Requested resolution {record['requested_resolution']}. "
            f"Repeat defect on this product: {record['repeat_defect']}."
        )
        records.append(sanitize_record(record))

    return records


# --- policies and guides ------------------------------------------------------


def generate_policy_documents(
    categories: pd.DataFrame,
    scored_sellers: pd.DataFrame,
) -> list[dict[str, Any]]:
    """Distinct policies, linked to sellers by the categories those sellers sell.

    Two things v1 got wrong (AUDIT.md F-15): 73 of the 79 policies were one
    sentence with the category name swapped -- near-duplicates in cosine space,
    so top-5 retrieval returned five interchangeable paragraphs -- and policies
    linked to no seller at all, while the flagship question asks for "sellers
    associated with relevant support policies".
    """
    # Category groups keep the policy count low and each document distinct.
    category_groups = {
        "electronics_and_computing": [
            "informatica_acessorios", "eletronicos", "pc_gamer", "telefonia",
            "telefonia_fixa", "tablets_impressao_imagem", "consoles_games",
            "audio", "eletroportateis", "eletrodomesticos", "eletrodomesticos_2",
        ],
        "home_and_furniture": [
            "moveis_decoracao", "casa_conforto", "casa_conforto_2", "cama_mesa_banho",
            "utilidades_domesticas", "moveis_escritorio", "moveis_sala", "moveis_quarto",
            "moveis_cozinha_area_de_servico_jantar_e_jardim", "portateis_casa_forno_e_cafe",
            "portateis_cozinha_e_preparadores_de_alimentos", "la_cuisine",
        ],
        "fashion_and_personal": [
            "beleza_saude", "perfumaria", "relogios_presentes", "fashion_bolsas_e_acessorios",
            "fashion_calcados", "fashion_roupa_masculina", "fashion_roupa_feminina",
            "fashion_underwear_e_moda_praia", "fashion_esporte", "fashion_roupa_infanto_juvenil",
        ],
    }
    group_of_category: dict[str, str] = {}
    for group, members in category_groups.items():
        for member in members:
            group_of_category[member] = group

    known_categories = set(categories["product_category_name"])
    sellers_by_group: dict[str, list[str]] = {group: [] for group in category_groups}
    sellers_by_category: dict[str, list[str]] = {}

    for seller_id, category in zip(
        scored_sellers["seller_id"], scored_sellers["dominant_category"]
    ):
        if category is None or pd.isna(category):
            continue
        sellers_by_category.setdefault(category, []).append(seller_id)
        group = group_of_category.get(category)
        if group:
            sellers_by_group[group].append(seller_id)

    # A deterministic, dated effective_from per policy, derived from the seed.
    base_date = datetime(2017, 1, 1, tzinfo=timezone.utc)

    records: list[dict[str, Any]] = []
    index = 0

    for topic_key, topic_title in POLICY_TOPICS:
        for scope, scope_label in [
            ("all_sellers", "all sellers"),
            ("category_scoped", None),
            ("high_volume_sellers", "sellers above the 90th percentile by order volume"),
        ]:
            if scope == "category_scoped":
                scopes = list(category_groups)
            else:
                scopes = [None]

            for group in scopes:
                index += 1
                rng = record_rng("policy", topic_key, scope, group or "-")

                if scope == "category_scoped":
                    applies_to = sorted(set(sellers_by_group[group]))
                    scope_text = f"sellers whose primary category is in {group.replace('_', ' ')}"
                    in_scope_categories = sorted(
                        c for c in category_groups[group] if c in known_categories
                    )
                    title = f"{topic_title} — {group.replace('_', ' ').title()}"
                elif scope == "high_volume_sellers":
                    threshold = scored_sellers["n_orders"].quantile(0.90)
                    applies_to = sorted(
                        scored_sellers.loc[
                            scored_sellers["n_orders"] >= threshold, "seller_id"
                        ]
                    )
                    scope_text = scope_label
                    in_scope_categories = []
                    title = f"{topic_title} — High Volume Sellers"
                else:
                    applies_to = sorted(scored_sellers["seller_id"])
                    scope_text = scope_label
                    in_scope_categories = []
                    title = f"{topic_title} — All Sellers"

                body = POLICY_BODIES[topic_key](scope_text, rng)
                exception = rng.choice(POLICY_EXCEPTIONS[topic_key])
                effective_from = (
                    base_date + timedelta(days=int(stable_hash_unit("policy_date", index) * 900))
                ).date().isoformat()

                record = {
                    "document_id": generate_id("POL", index),
                    "artifact_type": "policy_documents",
                    "policy_topic": topic_key,
                    "applicability_scope": scope,
                    "title": title,
                    "effective_from": effective_from,
                    "policy_text": body,
                    "exception_clause": exception,
                    "seller_count_in_scope": len(applies_to),
                    "created_at": f"{effective_from}T00:00:00+00:00",
                    # linked_entities is flattened into Neo4j node properties by
                    # synthetic_neo4j_loader.flatten_record, so it carries scalars
                    # only. The seller and category lists live in metadata, which
                    # the loader JSON-encodes as a whole.
                    "metadata": {
                        # An "all sellers" policy applies to everyone by
                        # definition, so enumerating 3,030 IDs would add ~800 KB
                        # to the corpus and 24k graph edges that discriminate
                        # nothing. The rule is stored instead; the scoped
                        # policies -- the ones the flagship question needs --
                        # carry their explicit seller set.
                        "seller_ids": [] if scope == "all_sellers" else applies_to,
                        "in_scope_categories": in_scope_categories,
                        "seller_selection_rule": scope,
                    },
                    "linked_entities": {
                        "category_group": group,
                        "applicability_scope": scope,
                        "seller_count_in_scope": len(applies_to),
                    },
                }
                record["summary"] = (
                    f"{topic_title} policy applying to {scope_text} "
                    f"({len(applies_to)} sellers)"
                )
                record["document_text"] = (
                    f"Policy {record['document_id']}: {title}. "
                    f"Effective from {effective_from}. Applies to {scope_text}, "
                    f"covering {len(applies_to)} sellers. {body} "
                    f"Exception: {exception}"
                )
                records.append(sanitize_record(record))

    return records


POLICY_BODIES = {
    "late_delivery_compensation": lambda scope, rng: (
        f"Where a delivery to {scope} exceeds the estimated date by more than five working "
        f"days, the support agent must offer shipping-cost compensation without requiring the "
        f"customer to ask. Delays attributable to the carrier are absorbed by the platform; "
        f"delays caused by late seller dispatch are recharged to the seller."
    ),
    "damaged_goods_returns": lambda scope, rng: (
        f"Damage reported by {scope} within 72 hours of delivery is settled by replacement, "
        f"with no return shipment required for items under 30 reais. Photographic evidence is "
        f"requested but its absence is not grounds for refusal where the carrier has logged a "
        f"handling exception."
    ),
    "warranty_claim_handling": lambda scope, rng: (
        f"Warranty claims from customers of {scope} are assessed within five working days. A "
        f"claim for a defect that has already been replaced once on the same product is "
        f"escalated to the seller account team rather than settled at first line."
    ),
    "payment_dispute_resolution": lambda scope, rng: (
        f"Payment disputes raised against {scope} are held in a non-settling state until the "
        f"instalment schedule and any voucher application have been reconciled against the "
        f"authorisation record. Duplicate authorisations are released without customer request."
    ),
    "seller_performance_review": lambda scope, rng: (
        f"Performance review for {scope} runs monthly against late-delivery rate, unresolved "
        f"ticket ratio and repeat-defect count. A seller whose unresolved ratio exceeds the "
        f"platform median for two consecutive months enters a supported improvement plan "
        f"before any listing restriction is applied."
    ),
    "escalation_and_ombudsman": lambda scope, rng: (
        f"A case involving {scope} that remains unresolved after three customer contacts is "
        f"escalated to operations review. Cases where the customer has cited a regulatory "
        f"body are routed to the ombudsman desk on the same day, regardless of severity."
    ),
    "fragile_goods_packaging": lambda scope, rng: (
        f"Sellers within {scope} shipping fragile items must use inner suspension packaging "
        f"rated for a one-metre drop. Repeated damage-in-transit claims against the same "
        f"seller trigger a packaging audit before any compensation is recharged."
    ),
    "repeat_complaint_handling": lambda scope, rng: (
        f"Where the same customer of {scope} raises a third complaint about the same product "
        f"within ninety days, the case is consolidated and handled by a single named agent. "
        f"Consolidated cases bypass the standard first-line script."
    ),
}

POLICY_EXCEPTIONS = {
    "late_delivery_compensation": [
        "Compensation does not apply where the delay is caused by an incomplete delivery address supplied by the customer.",
        "Force-majeure events logged by the carrier suspend this policy for the affected region.",
    ],
    "damaged_goods_returns": [
        "Items marked as clearance stock are excluded from no-return replacement.",
        "Damage reported after 30 days is handled under the warranty policy instead.",
    ],
    "warranty_claim_handling": [
        "Consumable components are excluded unless failure occurs within 14 days.",
        "Claims on products withdrawn from the catalogue are settled by refund only.",
    ],
    "payment_dispute_resolution": [
        "Disputes already raised with the card issuer are paused pending the issuer's decision.",
        "Voucher-only orders are refunded as store credit rather than to a payment method.",
    ],
    "seller_performance_review": [
        "Sellers with fewer than ten orders in the review window are excluded from ranking.",
        "A seller may appeal once per quarter with supporting carrier evidence.",
    ],
    "escalation_and_ombudsman": [
        "Cases withdrawn by the customer are closed without escalation.",
        "Escalation is skipped where the seller has already offered full remedy.",
    ],
    "fragile_goods_packaging": [
        "Items shipped in manufacturer-sealed retail packaging are exempt from the suspension requirement.",
        "Audits are deferred where damage is concentrated in a single carrier lane.",
    ],
    "repeat_complaint_handling": [
        "Complaints about distinct products are not consolidated even from the same customer.",
        "Consolidation does not extend the standard resolution deadline.",
    ],
}


GUIDE_SYMPTOMS = [
    "customer reports the item never arrived despite a delivered scan",
    "item arrives with visible external carton damage",
    "product powers on but fails intermittently",
    "parts listed in the description are missing from the box",
    "customer reports the charge does not match the checkout total",
    "product shows wear inconsistent with its stated age",
]

GUIDE_STEPS = [
    "Confirm the order identifier and the delivery scan history before contacting the seller.",
    "Request photographs of the packaging as received, including the shipping label.",
    "Check whether the same product has an open repeat-defect flag on another claim.",
    "Verify the payment authorisation and any voucher application against the order total.",
    "Compare the delivery date against the estimate and record the delay in days.",
    "Establish whether the fault sits with the carrier, the seller, or the customer address.",
    "Offer the remedy the applicable policy permits before escalating to tier 2.",
    "Record the root cause on the ticket so the seller performance review can use it.",
]


def generate_troubleshooting_guides(categories: pd.DataFrame) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []

    for index, (_, row) in enumerate(categories.iterrows(), start=1):
        category = row["product_category_name"]
        category_english = row["product_category_name_english"]
        rng = record_rng("guide", category)

        symptoms = rng.sample(GUIDE_SYMPTOMS, 3)
        steps = rng.sample(GUIDE_STEPS, 4)

        record = {
            "guide_id": generate_id("GDE", index),
            "artifact_type": "troubleshooting_guides",
            "title": f"Troubleshooting Guide: {category_english.replace('_', ' ').title()}",
            "category_name": category,
            "category_name_english": category_english,
            "symptoms": symptoms,
            "procedure_steps": steps,
            "created_at": (
                datetime(2017, 6, 1, tzinfo=timezone.utc)
                + timedelta(days=int(stable_hash_unit("guide_date", category) * 700))
            ).date().isoformat() + "T00:00:00+00:00",
            "linked_entities": {
                "category_id": category,
                "category_name_english": category_english,
            },
        }
        record["summary"] = (
            f"Diagnostic procedure for {category_english.replace('_', ' ')} "
            f"covering {len(symptoms)} symptom classes"
        )
        record["document_text"] = (
            f"Troubleshooting Guide {record['guide_id']} for category "
            f"{category_english} ({category}). "
            f"Symptoms: {'; '.join(symptoms)}. "
            f"Procedure: {' '.join(steps)}"
        )
        records.append(sanitize_record(record))

    return records


# --- document length guard ----------------------------------------------------


def check_document_lengths(artifact_sets: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Assert every document_text fits the embedding window with margin.

    docs/CORPUS_DESIGN.md Part 3 D-6. The authored text has always fitted; the
    763 truncations AUDIT.md F-10 measured are created by qdrant_ingest
    rebuilding the string. This guard makes the M1 side of that contract explicit
    so that when M2 stops rebuilding, the guarantee is already in place.

    Falls back to a whitespace estimate where transformers is unavailable, so
    generation never depends on a model download.
    """
    limit = int(MAX_DOCUMENT_WORDPIECES * WORDPIECE_SAFETY_MARGIN)
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(
            os.getenv("EMBEDDING_MODEL_NAME", "sentence-transformers/all-MiniLM-L6-v2"),
            revision=os.getenv(
                "EMBEDDING_MODEL_REVISION", "1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
            ),
        )

        def count(value: str) -> int:
            return len(tokenizer.encode(value))

        method = "wordpiece"
    except Exception:  # noqa: BLE001 -- length guard must not block generation
        def count(value: str) -> int:
            return int(len(value.split()) * 1.4)

        method = "whitespace_estimate"

    report: dict[str, Any] = {"method": method, "limit": limit, "groups": {}}
    violations: list[str] = []

    for name, records in artifact_sets.items():
        lengths = [count(record["document_text"]) for record in records]
        longest = max(lengths) if lengths else 0
        over = [
            records[i]["document_text"][:60]
            for i, length in enumerate(lengths)
            if length > limit
        ]
        report["groups"][name] = {
            "count": len(lengths),
            "median_tokens": int(np.median(lengths)) if lengths else 0,
            "max_tokens": longest,
            "over_limit": len(over),
        }
        if over:
            violations.append(f"{name}: {len(over)} documents over {limit} (max {longest})")

    if violations:
        raise ValueError(
            "document_text exceeds the embedding window: "
            + "; ".join(violations)
            + f". The window is {MAX_DOCUMENT_WORDPIECES} word-pieces and the guard "
            f"allows {int(WORDPIECE_SAFETY_MARGIN * 100)}% of it."
        )

    return report


# --- orchestration ------------------------------------------------------------


def generate_synthetic_data(
    output_dir: Path,
    report_path: Path,
    support_ticket_count: int,
    logistics_incident_count: int,
    warranty_claim_count: int,
    seed: int,
) -> dict[str, Any]:
    engine = build_postgres_engine()

    try:
        seller_attrs = fetch_seller_attributes(engine)
        seller_orders = fetch_seller_orders(engine)
        categories = fetch_categories(engine)
    finally:
        engine.dispose()

    scored = score_sellers(seller_attrs)
    latent_by_seller = dict(zip(scored["seller_id"], scored["latent_ops_trait"]))

    available_orders = seller_orders.groupby("seller_id").size()
    complaint_counts = allocate_complaints(scored, support_ticket_count, available_orders)
    selected_orders = select_complaint_orders(scored, complaint_counts, seller_orders)

    support_tickets = generate_support_tickets(selected_orders, latent_by_seller)
    customer_emails = generate_customer_emails(support_tickets)
    logistics_incidents = generate_logistics_incidents(
        seller_orders, latent_by_seller, logistics_incident_count
    )
    ticket_id_by_order = {
        ticket["linked_entities"]["order_id"]: ticket["ticket_id"]
        for ticket in support_tickets
    }
    warranty_claims = generate_warranty_claims(
        seller_orders, scored, latent_by_seller, warranty_claim_count, ticket_id_by_order
    )
    policy_documents = generate_policy_documents(categories, scored)
    troubleshooting_guides = generate_troubleshooting_guides(categories)

    artifact_sets = {
        "support_tickets": support_tickets,
        "logistics_incidents": logistics_incidents,
        "customer_emails": customer_emails,
        "warranty_claims": warranty_claims,
        "policy_documents": policy_documents,
        "troubleshooting_guides": troubleshooting_guides,
    }

    length_report = check_document_lengths(artifact_sets)

    output_files: dict[str, str] = {}
    file_hashes: dict[str, str] = {}
    for artifact_name, records in artifact_sets.items():
        records = [promote_entity_ids(record) for record in records]
        artifact_sets[artifact_name] = records
        output_path = output_dir / f"{artifact_name}.jsonl"
        write_jsonl(output_path, records)
        output_files[artifact_name] = str(output_path)
        file_hashes[artifact_name] = hashlib.sha256(
            output_path.read_bytes()
        ).hexdigest()

    corpus_hash = hashlib.sha256(
        "|".join(f"{name}:{file_hashes[name]}" for name in sorted(file_hashes)).encode()
    ).hexdigest()

    report = {
        # No datetime.now() anywhere in this report: Part 1 N-3. The corpus hash
        # identifies the run instead of the wall clock.
        "corpus_hash": corpus_hash,
        "corpus_seed": CORPUS_SEED,
        "seed": seed,
        "output_dir": str(output_dir),
        "parameters": {
            "deficit_weight": DEFICIT_WEIGHT,
            "intensity_exponent": INTENSITY_EXPONENT,
            "volume_exponent": VOLUME_EXPONENT,
        },
        "summary": {
            "artifact_type_count": len(artifact_sets),
            "total_records": sum(len(records) for records in artifact_sets.values()),
            "sellers_with_complaints": int((complaint_counts > 0).sum()),
            "sellers_scored": int(len(scored)),
        },
        "document_lengths": length_report,
        "artifacts": {
            artifact_name: {
                "record_count": len(records),
                "output_file": output_files[artifact_name],
                "sha256": file_hashes[artifact_name],
            }
            for artifact_name, records in artifact_sets.items()
        },
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate the synthetic enterprise corpus deterministically."
    )
    parser.add_argument("--output-dir", type=Path, default=Path("data/synthetic"))
    parser.add_argument(
        "--report", type=Path, default=Path("reports/synthetic_generation_report.json")
    )
    parser.add_argument("--support-ticket-count", type=int, default=3000)
    parser.add_argument("--logistics-incident-count", type=int, default=1000)
    parser.add_argument("--warranty-claim-count", type=int, default=1000)
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Recorded in the report. The corpus identity comes from CORPUS_SEED.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    report = generate_synthetic_data(
        output_dir=args.output_dir,
        report_path=args.report,
        support_ticket_count=args.support_ticket_count,
        logistics_incident_count=args.logistics_incident_count,
        warranty_claim_count=args.warranty_claim_count,
        seed=args.seed,
    )

    print("\nSynthetic Data Generation Completed")
    print("-----------------------------------")
    print(f"Corpus hash: {report['corpus_hash']}")
    print(f"Artifact types: {report['summary']['artifact_type_count']}")
    print(f"Total records: {report['summary']['total_records']}")
    print(f"Sellers with complaints: {report['summary']['sellers_with_complaints']}")
    print(f"Report saved to: {args.report}")

    print("\nGenerated artifacts:")
    for artifact_name, artifact_info in report["artifacts"].items():
        print(
            f"{artifact_name}: {artifact_info['record_count']} records -> "
            f"{artifact_info['output_file']}"
        )


if __name__ == "__main__":
    main()
