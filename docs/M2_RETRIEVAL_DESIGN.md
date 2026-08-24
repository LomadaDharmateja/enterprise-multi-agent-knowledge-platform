# M2 Task 3 — parameterising the retrieval templates (F-03)

**Status: approved and implemented (M2 Task 3).** Decisions D-1..D-4 were taken as
recommended; §8 records what implementation changed about the design.

The rule this design implements, stated at review:

> The allowlist constrains the shape (which template is called), the planner provides
> the values (which entities, filters, thresholds). The LLM never writes a query.

Everything below is measured against that. Where a choice weakens it, the choice is
flagged rather than taken quietly.

---

## 0. Preliminaries

### 0.1 Template names

The task brief names `product_analysis` and `order_analysis`. The templates are
actually called `product_performance` and `order_summary`, and those names are frozen
byte-for-byte by `tests/test_allowlist_frozen.py`. This design keeps the real names.

| brief | actual |
|---|---|
| `product_analysis` | `product_performance` |
| `order_analysis` | `order_summary` |
| `seller_performance` | `seller_performance` |

### 0.2 The binding mechanism

Two properties have to hold at once: filters must be optional, and each SQL string
must stay a **single static literal** so `test_allowlist_frozen.py` can keep freezing
it. Assembling `WHERE` clauses per request would satisfy the first and destroy the
second.

The idiom that satisfies both, **verified live against this database**:

```sql
WHERE (CAST(:seller_ids AS text[]) IS NULL OR seller_id = ANY(CAST(:seller_ids AS text[])))
```

A `NULL` bind neutralises the clause; a list applies it. One string, no concatenation.

The `CAST(... AS ...)` form is required — the PostgreSQL `::text[]` shorthand collides
with SQLAlchemy's `:param` syntax and fails to bind at all. Measured:

```
no filters  : 3 rows -> [('633ecdf8','SP',0.667), ('427165bf','SP',0.6),   ('0873d9f8','SP',0.5)]
state=SP    : 3 rows -> [('633ecdf8','SP',0.667), ('427165bf','SP',0.6),   ('0873d9f8','SP',0.5)]
state=RJ    : 3 rows -> [('30c7f28f','RJ',0.429), ('83b08de9','RJ',0.4),   ('3e8bd881','RJ',0.333)]
2 seller_ids: 2 rows -> [('06a2c3af','MA',0.23),  ('5145090a','PR',0.0)]
```

Cypher has the same idiom and it also works:

```cypher
WHERE ($seller_ids IS NULL OR s.seller_id IN $seller_ids)
```

### 0.3 Sorting without letting the LLM write SQL

`ORDER BY` cannot be a bind parameter. Two options:

- **(a)** keep a dict of pre-written `ORDER BY` fragments keyed by an allowlisted
  identifier, and concatenate the chosen one into the template;
- **(b)** write every sort key as a `CASE` inside one static `ORDER BY`, selected by
  a `:sort_by` bind.

**Recommended: (b).** No LLM-derived text ever enters a SQL string, and every template
stays a literal that a test can hash. Option (a) is safe in practice but it moves the
templates out of reach of the freeze test, and the allowlist is the one control the
audit found sound. The cost is verbosity and a slightly worse query plan. Shape:

```sql
ORDER BY
  CASE WHEN :sort_by = 'late_delivery_rate' THEN
       CASE WHEN total_orders > 0 THEN late_delivery_orders::numeric / total_orders END END DESC NULLS LAST,
  CASE WHEN :sort_by = 'total_item_revenue'     THEN total_item_revenue END DESC NULLS LAST,
  CASE WHEN :sort_by = 'avg_review_score_worst' THEN avg_review_score   END ASC  NULLS LAST,
  seller_id ASC
```

The trailing `seller_id ASC` is a unique tiebreaker, on every template. This is the
same determinism requirement M1 imposed on the generator (`docs/CORPUS_DESIGN.md`
Part 1, N-1) applied to retrieval: without it, ties reorder between runs and Task 5's
evidence diff becomes noise.

### 0.4 Where values come from

Two sources, in order of precedence:

1. **Planner extraction** — Gemini reads the question and proposes filters and a sort
   key, in the same single call that already produces the routes.
2. **Deterministic resolution** — a code-owned validator that checks every proposed
   value against a per-template parameter allowlist and a live vocabulary, coercing or
   **dropping** anything that does not resolve. Nothing unresolved reaches a database,
   and everything dropped is recorded in `dropped_filters` on the plan.

Step 2 is not optional. Bind parameters make injection impossible, but they do not
stop the LLM inventing `product_category = "furniture"` — not a value in this dataset —
and silently returning zero rows. The vocabulary is read once from PostgreSQL:

| vocabulary | size | values / source |
|---|---|---|
| product categories (pt + en) | 73 × 2 | `ecommerce.product_category_translations` |
| seller states | 23 | `ecommerce.sellers` |
| order statuses | 8 | `approved, canceled, created, delivered, invoiced, processing, shipped, unavailable` |
| delivery statuses | 2 | `delivered, not_delivered` |
| ticket severities | 4 | `critical, high, medium, low` |
| ticket issue types | 6 | `delayed_delivery, missing_item, product_quality_complaint, negative_review_escalation, damaged_item, payment_question` |
| incident types | 6 | `seller_dispatch_delay, hub_missort, weather_disruption, carrier_backlog, address_error, customs_hold` |
| claim statuses | 4 | `rejected, approved, pending, withdrawn` |
| policy topics | 8 | `late_delivery_compensation, damaged_goods_returns, warranty_claim_handling, payment_dispute_resolution, seller_performance_review, escalation_and_ombudsman, fragile_goods_packaging, repeat_complaint_handling` |
| entity IDs | — | 32-hex regex; `TCK-` / `INC-` / `POL-` / `GDE-` prefix forms |

Dropping rather than passing through is deliberate. F-06 was that
`standardize_columns` silently invented missing columns; the same failure mode here
would be a filter that silently matches nothing and an answer confidently grounded on
an empty table.

---

## 1. SQL templates

Common to all six: `:limit`, a unique tiebreaker in `ORDER BY`, and a `:sort_by` enum
whose default is stated per template.

### 1.1 `seller_performance`

**Current**

```sql
SELECT seller_id, seller_state, seller_city, total_orders, total_items_sold,
       total_item_revenue, avg_review_score, review_count, late_delivery_orders
FROM ecommerce.vw_seller_performance
ORDER BY late_delivery_orders DESC NULLS LAST,
         review_count DESC NULLS LAST,
         total_item_revenue DESC NULLS LAST
LIMIT :limit;
```

No filter. `ORDER BY late_delivery_orders DESC` is an **absolute count**, so it ranks
the highest-volume sellers; the audit measured their average review scores at
3.83–4.13, above the dataset mean. The flagship complaint question is grounded on
evidence about well-rated sellers. That is F-03's sharpest consequence, and it is a
sort-order bug as much as a filter bug.

**Parameters**

| parameter | type | meaning |
|---|---|---|
| `seller_ids` | `text[] \| null` | restrict to named sellers |
| `seller_states` | `text[] \| null` | 2-letter state codes, resolved |
| `min_orders` | `int` (default **5**) | denominator guard for the rate |
| `max_avg_review_score` | `float \| null` | "poorly rated" threshold |
| `min_late_delivery_rate` | `float \| null` | "unreliable" threshold |
| `sort_by` | enum, default `late_delivery_rate` | `late_delivery_rate`, `avg_review_score_worst`, `total_item_revenue`, `total_orders`, `late_delivery_orders` |

`late_delivery_rate` is computed, since the view has no such column:

```sql
CASE WHEN total_orders > 0
     THEN late_delivery_orders::numeric / total_orders ELSE 0 END AS late_delivery_rate
```

`min_orders` defaults to 5 because a seller with one order and one late delivery has a
rate of 1.0 and is noise. Five is the same threshold `docs/CORPUS_DESIGN.md` Part 4
used for its quartile analysis, so the retrieval layer and the corpus statistics agree
on what counts as a rankable seller.

**The default sort changes from `late_delivery_orders` to `late_delivery_rate`.** This
is a judgement call, and it changes baseline evidence even for questions with no
filters. It is the change that makes "sellers with complaints" return unreliable
sellers instead of big ones.

**Extraction.** `sort_by` ← complaint/reliability wording ("complaints", "worst",
"unreliable", "late") → `late_delivery_rate`; revenue/size wording →
`total_item_revenue`. `seller_states` ← a named Brazilian state.
`max_avg_review_score` ← "poorly rated", "negative". `seller_ids` ← literal 32-hex IDs
in the question, which is rare: **sellers in this dataset have no human-readable
names**, so an explicit seller filter almost never fires from natural language. See
decision D-1 for the source that does fire.

**Bound example** — *"Find sellers with negative customer complaints, warranty issues,
and relevant support policies"*

```json
{"seller_ids": null, "seller_states": null, "min_orders": 5,
 "max_avg_review_score": 4.0, "min_late_delivery_rate": null,
 "sort_by": "late_delivery_rate", "limit": 10}
```

**Bound example** — *"Which seller had the highest revenue?"*

```json
{"seller_ids": null, "seller_states": null, "min_orders": 1,
 "max_avg_review_score": null, "min_late_delivery_rate": null,
 "sort_by": "total_item_revenue", "limit": 10}
```

Two questions, same template, provably different rows — this pair is the boundary test
in Task 3's exit criterion.

### 1.2 `product_performance`

**Current** — no filter; `ORDER BY review_count DESC, total_items_sold DESC`.

| parameter | type | meaning |
|---|---|---|
| `product_categories` | `text[] \| null` | English names, resolved from either language |
| `product_ids` | `text[] \| null` | named products |
| `min_orders` | `int` (default 1) | volume floor |
| `max_avg_review_score` | `float \| null` | quality threshold |
| `sort_by` | enum, default `total_product_revenue` | `total_product_revenue`, `total_items_sold`, `avg_review_score_worst`, `review_count` |

**Extraction.** `product_categories` ← any of the 146 category strings (73 pt + 73 en)
appearing in the question, plus a small synonym map for obvious English business words
("furniture" → `furniture_decor`, "electronics" → `computers_accessories`). Unmatched
category words are **dropped and recorded**, not guessed.

**Bound example** — *"Which seller in Rio de Janeiro sold the most furniture?"*

```json
{"product_categories": ["furniture_decor"], "product_ids": null,
 "min_orders": 1, "max_avg_review_score": null,
 "sort_by": "total_items_sold", "limit": 10}
```

### 1.3 `review_intelligence`

**Current** — `WHERE is_negative_review = TRUE` hardcoded; `ORDER BY review_score ASC`.

| parameter | type | meaning |
|---|---|---|
| `negative_only` | `bool` (default true) | promotes the hardcoded predicate to a parameter |
| `max_review_score` | `int \| null` | 1–5 |
| `customer_states` | `text[] \| null` | resolved |
| `order_ids` | `text[] \| null` | join key from another leg |
| `date_from` / `date_to` | `date \| null` | on `review_creation_date` |
| `sentiment_labels` | `text[] \| null` | non-NULL since M1's F-05 fix |
| `sort_by` | enum, default `review_score_worst` | `review_score_worst`, `review_creation_date_recent` |

**Bound example** — *"What were customers complaining about in São Paulo in 2018?"*

```json
{"negative_only": true, "max_review_score": 2, "customer_states": ["SP"],
 "order_ids": null, "date_from": "2018-01-01", "date_to": "2018-12-31",
 "sentiment_labels": null, "sort_by": "review_creation_date_recent", "limit": 10}
```

### 1.4 `payment_summary`

**Current** — no filter; `ORDER BY total_payment_value DESC`.

| parameter | type | meaning |
|---|---|---|
| `order_ids`, `customer_ids` | `text[] \| null` | join keys |
| `order_statuses` | `text[] \| null` | resolved against the 8 |
| `payment_type` | `text \| null` | matched against the aggregated `payment_types` column |
| `min_installments` | `int \| null` | |
| `sort_by` | enum, default `total_payment_value` | `total_payment_value`, `max_payment_installments` |

### 1.5 `customer_history`

**Current** — no filter; `ORDER BY total_orders DESC, total_customer_payment_value DESC`.

| parameter | type | meaning |
|---|---|---|
| `customer_unique_ids` | `text[] \| null` | |
| `min_orders` | `int` (default 1) | "repeat customers" |
| `min_late_delivery_orders` | `int \| null` | |
| `max_avg_review_score` | `float \| null` | |
| `sort_by` | enum, default `total_orders` | `total_orders`, `total_customer_payment_value`, `late_delivery_orders` |

### 1.6 `order_summary`

**Current** — no filter; `ORDER BY is_late_delivery DESC, delivery_delay_days DESC, total_payment_value DESC`.

| parameter | type | meaning |
|---|---|---|
| `order_ids` | `text[] \| null` | |
| `order_statuses` | `text[] \| null` | resolved against the 8 |
| `delivery_status` | `text \| null` | `delivered` / `not_delivered` |
| `customer_states` | `text[] \| null` | |
| `date_from` / `date_to` | `date \| null` | on `order_purchase_timestamp`; data spans 2016-09-04 → 2018-10-17 |
| `is_late_delivery` | `bool \| null` | |
| `min_delay_days` | `int \| null` | |
| `sort_by` | enum, default `delivery_delay_days` | `delivery_delay_days`, `total_payment_value`, `order_purchase_recent` |

**Bound example** — *"Which orders were cancelled in the last quarter of 2017?"*

```json
{"order_ids": null, "order_statuses": ["canceled"], "delivery_status": null,
 "customer_states": null, "date_from": "2017-10-01", "date_to": "2017-12-31",
 "is_late_delivery": null, "min_delay_days": null,
 "sort_by": "order_purchase_recent", "limit": 10}
```

---

## 2. Cypher templates

Three of the five have **no `ORDER BY` at all**, so their "top ten" is whatever the
planner happened to emit. All five get a deterministic order with a unique tiebreaker.

### 2.1 The severity ordering bug, confirmed live

`seller_ticket_product_paths` ends `ORDER BY t.severity DESC`. `severity` is a string,
so it sorts alphabetically. Measured against the live graph:

```
OLD  ORDER BY t.severity DESC   -> medium, medium, medium, medium
NEW  ORDER BY <severity rank>   -> critical, critical, critical
```

Replacement, used in every template that orders by severity:

```cypher
ORDER BY CASE t.severity WHEN 'critical' THEN 4 WHEN 'high' THEN 3
                         WHEN 'medium'   THEN 2 WHEN 'low'  THEN 1
                         ELSE 0 END DESC,
         t.ticket_id ASC
```

### 2.2 A blocker found while designing this: Neo4j `Category` nodes are stale

```
MATCH (c:Category) RETURN count(*), count(c.product_category_name_english)
-> 73, 0
keys(c) -> ["product_category_name", "category_id"]
```

Three of the five Cypher templates `RETURN c.product_category_name_english AS category`,
so **every graph result currently hands the LLM `category: null`**, and a category
filter on the English name would match nothing.

This is F-04's residue. The loader code is correct — `neo4j_loader.py:43-50` selects
the column — but the graph was loaded while that column was NULL for all 73 rows
(F-04, fixed in PostgreSQL in M1), and Neo4j drops null properties on `SET n += row`,
so the key was never created. PostgreSQL now has 73 of 73. **Re-running the Neo4j
Category load repairs it; no code change is needed.** See decision D-2.

Until then, category filtering binds `category_id` (the Portuguese slug, populated on
all 73 nodes), which is the more robust key anyway — the planner resolves either
language to a canonical `category_id` through the translation table.

### 2.3 Per-template parameters

| template | current order | parameters and new order |
|---|---|---|
| `warranty_product_seller_paths` | **none** | `$seller_ids`, `$category_ids`, `$severities`, `$claim_statuses` → severity rank DESC, `claim_id` |
| `logistics_region_paths` | `o.delivery_delay_days DESC` (no tiebreaker) | `$seller_ids`, `$region_states`, `$incident_types`, `$severities`, `$min_delay_days` → delay DESC, `incident_id` |
| `category_policy_guide_paths` | **none** | `$category_ids`, `$policy_topics`, `$seller_ids` (via `APPLIES_TO_SELLER`) → `category_id`, `policy_document_id` |
| `seller_ticket_product_paths` | `t.severity DESC` (**wrong**) | `$seller_ids`, `$category_ids`, `$issue_types`, `$severities` → severity rank DESC, `ticket_id` |
| `customer_ticket_order_product_paths` | **none** | `$customer_ids`, `$order_ids`, `$category_ids`, `$issue_types`, `$severities` → severity rank DESC, `ticket_id` |

**Bound example** — `seller_ticket_product_paths`, *"Show seller ticket paths for
negative complaints and product issues"*

```cypher
MATCH (s:Seller)<-[:INVOLVES_SELLER]-(t:SupportTicket)-[:MENTIONS_PRODUCT]->(p:Product)
      -[:BELONGS_TO_CATEGORY]->(c:Category)
WHERE ($seller_ids   IS NULL OR s.seller_id   IN $seller_ids)
  AND ($category_ids IS NULL OR c.category_id IN $category_ids)
  AND ($issue_types  IS NULL OR t.issue_type  IN $issue_types)
  AND ($severities   IS NULL OR t.severity    IN $severities)
RETURN ...
ORDER BY <severity rank> DESC, t.ticket_id ASC
LIMIT $limit;
```

```json
{"seller_ids": null, "category_ids": null,
 "issue_types": ["product_quality_complaint", "negative_review_escalation"],
 "severities": ["critical", "high"], "limit": 10}
```

Verified live: unfiltered returns `critical` tickets first once rank ordering is in
place; `severities=["critical"]` returns only criticals; a category filter on the
English name returns 0 rows today, which is §2.2.

**`APPLIES_TO_SELLER` caveat.** Binding `$seller_ids` on `category_policy_guide_paths`
uses the 15,072 edges M1 created. The 8 `all_sellers` policies store a *selection rule*
rather than enumerated edges (`docs/CORPUS_DESIGN.md` Part 4), so a naive seller filter
would exclude them even though they apply. The predicate must be: a policy matches if
it has an edge to one of `$seller_ids` **or** its scope is `all_sellers`.

---

## 3. Planner output schema, before and after

### Before

```json
{
  "planner": "gemini_query_planner",
  "planning_mode": "llm_tool_routing",
  "sql_intent": "seller_performance",
  "graph_intent": "warranty_product_seller_paths",
  "vector_artifact_groups": ["support_tickets"],
  "reasoning": "...",
  "generated_at": "...", "model": "...", "query": "...", "raw_response": "..."
}
```

`sql_intent` and `graph_intent` are required non-null; `vector_artifact_groups` must be
non-empty. There are **no values of any kind** — which is F-03 restated as a schema.
`validate_and_normalize_plan` (`gemini_query_planner.py:186-234`) raises if any of the
three is missing.

### After

```json
{
  "planner": "gemini_query_planner",
  "planning_mode": "llm_tool_routing",
  "plan_schema_version": 2,

  "answerable": true,
  "refusal_reason": null,

  "sql_intent": "seller_performance",
  "graph_intent": "warranty_product_seller_paths",
  "vector_artifact_groups": ["support_tickets", "warranty_claims", "policy_documents"],

  "sql_plan": {
    "intent": "seller_performance",
    "filters": {"min_orders": 5, "max_avg_review_score": 4.0},
    "sort_by": "late_delivery_rate"
  },
  "graph_plan": {
    "intent": "warranty_product_seller_paths",
    "filters": {"severities": ["critical", "high"]}
  },
  "vector_plan": {
    "artifact_groups": ["support_tickets", "warranty_claims", "policy_documents"]
  },

  "dropped_filters": [
    {"leg": "sql", "key": "product_categories", "value": "furniture",
     "reason": "not in category vocabulary"}
  ],

  "reasoning": "...",
  "generated_at": "...", "model": "...", "query": "...", "raw_response": "..."
}
```

Changes:

- **`answerable` / `refusal_reason`** — a question with nothing to route becomes a
  valid plan instead of an exception.
- **`sql_intent` / `graph_intent` nullable** — §4.
- **`sql_plan` / `graph_plan` / `vector_plan`** — the values, separated from the routes.
- **`dropped_filters`** — every value the resolver rejected, with the reason. Visible
  in the observability span rather than discarded.
- **the flat `sql_intent` / `graph_intent` / `vector_artifact_groups` keys are
  retained deliberately.** `agentic_workflow.py:114-116`, the observability spans and
  the baseline comparison all read them; keeping them means Task 5's diff shows
  evidence changes rather than schema churn.

The allowlist itself does **not** change: still 6 SQL intents, 5 graph intents, 6
vector groups, and `tests/test_allowlist_frozen.py` should keep passing untouched.
Filter keys get their own per-intent allowlist alongside it.

---

## 4. SQL-only and graph-only plans

The crash, from the frozen baseline:

```
control_highest_revenue_seller   "Which seller had the highest revenue?"
  ValueError: Invalid graph intent from Gemini: None.
  Allowed: ['warranty_product_seller_paths', ...]
```

Gemini answered correctly — the question needs no graph traversal — and the schema made
that answer unrepresentable.

**Current validation** (`gemini_query_planner.py:191-205`):

```python
if sql_intent not in ALLOWED_SQL_INTENTS:
    raise ValueError(...)
if graph_intent not in ALLOWED_GRAPH_INTENTS:
    raise ValueError(...)
if not isinstance(vector_artifact_groups, list) or not vector_artifact_groups:
    raise ValueError(...)
```

**Replacement:**

```python
# null is a legal answer; a non-null value must still be on the allowlist
if sql_intent is not None and sql_intent not in ALLOWED_SQL_INTENTS:
    raise ValueError(...)
if graph_intent is not None and graph_intent not in ALLOWED_GRAPH_INTENTS:
    raise ValueError(...)
for group in vector_artifact_groups or []:
    if group not in ALLOWED_VECTOR_GROUPS:
        raise ValueError(...)

# an answerable plan must reach at least one source
if answerable and not (sql_intent or graph_intent or vector_artifact_groups):
    raise ValueError("An answerable plan must select at least one retrieval leg.")
```

The allowlist is not loosened: an invalid *name* still raises. Only the requirement
that all three legs be populated is dropped.

Downstream, `run_hybrid_retrieval` must **skip** a null leg rather than falling back to
the keyword detectors (`detect_sql_intent` / `detect_graph_intent`). Falling back would
turn the crash into a wrong answer, which is worse. A skipped leg is reported as:

```json
{"source": "neo4j", "intent": null, "record_count": 0, "records": [], "skipped": true}
```

so the context builder and the answer prompt can state that the leg was not used
rather than implying it returned nothing. Task 4 verifies that.

---

## 5. What `"purple monkey dishwasher"` returns

**An explicit refusal.**

The audit's byte-identical finding was measured by calling `sql_retrieve` and
`graph_retrieve` directly. Through the full workflow the control does not currently
return identical evidence — **it crashes**, and the crash message is itself the
evidence for this design:

```
control_purple_monkey_dishwasher
  ValueError: Invalid SQL intent from Gemini: None.
```

Gemini already returns null routes for it. The planner is already recognising that
nothing applies; the schema could not represent that answer, so it raised. With §3 and
§4 in place, the same response becomes `answerable: false` with a `refusal_reason`,
**no database is queried**, and the workflow returns a refusal instead of prose.

Two honest caveats:

- Gemini is non-deterministic. On some runs it may route the control to a plausible
  intent, get an unfiltered query, and return the same evidence as another unfiltered
  question. The exit criterion is therefore correctly stated as *refusal **or**
  different evidence*, and Task 3 will report which actually occurred rather than
  asserting the outcome it prefers.
- A refusal is only meaningful if refusal is not the default. The test asserts both
  directions: the control refuses, and the flagship question does not.

---

## 6. Decisions needed before implementation

### D-1 — Evidence-linked filtering: should the vector leg feed the other two?

Sellers in this dataset have **no names**, only 32-hex IDs, so an explicit `seller_ids`
filter almost never fires from a natural question. Planner extraction alone will
differentiate questions by category, state, status, date, threshold and sort — real,
but it leaves the flagship question's SQL leg still unfiltered by seller.

The alternative is to reorder the legs: run **vector first**, harvest
`seller_id` / `order_id` / `product_id` / `customer_id` from the top hits, and bind them
into the SQL and graph legs when the planner supplied no explicit entity filter.

That makes the SQL leg question-dependent for *every* question, because the vector leg
always is. It is also the first thing that actually spends F-01's fix: "a vector hit
joins to a SQL row by entity ID" stops being a test assertion and becomes how retrieval
works. The flagship question would return SQL rows for the sellers the complaint
documents actually name, instead of the ten largest sellers.

Costs: the three legs stop being independent, so a vector-leg failure propagates; and
it is a larger change than "bind the planner's values".

**Recommendation: adopt it, behind a flag, default on for `seller_performance` and the
seller-bearing graph templates.** It is the difference between templates that *can* be
filtered and a system that *is* filtered.

### D-2 — Re-run the Neo4j Category load?

Needed for non-null `category` in graph evidence and for English-name filtering (§2.2).
No code change, one loader run. **Recommendation: yes**, recorded in
`docs/M2_CHANGES.md` as an environment change so Task 5's diff stays attributable.

### D-3 — Static `CASE` sort (§0.3 option b) or assembled fragments (option a)?

**Recommendation: static `CASE`**, to keep every template a hashable literal.

### D-4 — Add new allowlist intents?

A dedicated policy→seller template would serve the flagship question better than
reusing `category_policy_guide_paths`. **Recommendation: no.** M2 binds values to
existing shapes; widening the allowlist is a separate decision needing its own
adversarial re-test, and `test_allowlist_frozen.py` exists to make that deliberate.

---

## 7. Tests Task 3 will add

| test | asserts |
|---|---|
| `test_same_template_different_filters` | two questions routed to `seller_performance` with different `sort_by` return non-identical evidence sets — the Task 3 boundary test |
| `test_control_question_refuses_or_differs` | `"purple monkey dishwasher"` returns a refusal **or** evidence different from the flagship question; reports which |
| `test_revenue_question_does_not_crash` | `"Which seller had the highest revenue?"` completes with `graph_intent: null` |
| `test_sql_only_plan_is_representable` | schema-level: `validate_and_normalize_plan` accepts a null graph intent and still rejects an unknown one |
| `test_filter_values_are_resolved_or_dropped` | an invented category never reaches the database and appears in `dropped_filters` |
| `test_severity_orders_by_rank_not_alphabetically` | `critical` sorts above `medium` |
| `test_templates_remain_static_literals` | no template string is built by concatenation or f-string, keeping D-3 honest |
| `test_allowlist_frozen` (existing) | unchanged and still passing |

Each will be verified to fail on the unfixed code before being reported as passing, as
in Tasks 1 and 2.

---

# 8. Implementation notes — what changed from the design

Recorded here rather than silently absorbed, because three of these were found only
by running the thing.

## 8.1 `EVIDENCE_LINKED_FILTERING` — the D-1 flag

**What it does.** When on (the default), the vector leg runs first and its top hits
are mined for `seller_id`, `order_id`, `product_id` and `customer_id`. Those IDs are
bound into the SQL and graph legs wherever the planner supplied no entity filter of
its own — it never overrides an explicit planner value. Capped at 25 IDs per key. The
mapping from harvested key to template parameter is fixed in code
(`SQL_EVIDENCE_LINKS` / `GRAPH_EVIDENCE_LINKS` in `hybrid_retriever.py`); a template
not listed there is never filled from retrieved evidence.

**How to turn it off.** `EVIDENCE_LINKED_FILTERING=false` in the environment, or pass
`evidence_linked_filtering=False` to `run_hybrid_retrieval` — an explicit argument
always beats the environment. Off, the three legs are independent exactly as before.

**Tested in both states.** `test_flag_off_leaves_the_legs_independent` and
`test_flag_off_reproduces_unlinked_evidence` in `tests/test_retrieval_binding.py`
pin the off behaviour, so the independent-legs baseline stays measurable.

**Measured on the M2 baseline set (see `docs/M2_CHANGES.md` §5).** Evidence links were
bound on **7 of 9** answerable questions, and the empty-result fallback fired on **1**
(the flagship). Two questions with identical routes, identical planner filters and an
identical sort key return different SQL evidence *only* because their harvested
`order_ids` differ — without linking they would be byte-identical, which is the F-03
pattern. An earlier note here said linking "bought nothing" based on the flagship alone;
that single observation was unrepresentative and is corrected.

**The real delta is still M3's.** Nine questions are not held out, and several are the
planner's own few-shot examples. M3 will report, over the 60–100 item held-out set:

- the share of questions where linking changed the SQL evidence at all;
- the share where it triggered the empty-result fallback (linking bought nothing);
- retrieval precision with the flag on versus off, on the same questions;
- whether groundedness moves in the same direction as precision.

If linking does not beat the independent legs on that set, the honest outcome is to
default the flag to off and say so — the same standard M4 applies to replanning.

## 8.2 New: evidence linking must never empty a leg

Found by running the flagship question end to end. The planner asked for
`max_avg_review_score <= 3.0`; linking added nine seller IDs harvested from the
vector hits; the conjunction matched nothing, and both the SQL and graph legs
returned **0 records** where they had returned 10.

Neither filter was wrong on its own. The intersection was empty.

Linking is an enrichment the question did not ask for, so it must not be the reason a
leg comes back empty. A leg that returns nothing **and** had linked IDs applied is
re-run with only the planner's own filters, and the fallback is recorded in the
confidence signal:

> postgresql returned nothing for the entity IDs found in the retrieved documents, so
> it was re-run without them; these rows are not restricted to the entities the
> documents named

The planner's own filters may still legitimately return nothing — that is the user's
question answering itself, and it is not overridden.

## 8.3 Corrected: `sort_by` is not a filter

The first implementation counted the sort key toward `filters_proposed`, so
"Which seller had the highest revenue?" — no filters, one sort key — scored as
*every filter dropped* and was flagged low confidence. Wrong: an unresolvable sort
key falls back to a sensible default, whereas an unresolvable filter silently widens
the result set. Only filters count toward the signal now.

## 8.4 Limitation: the policy template reaches only 24 of 40 policies

The `all_sellers` (8) and `high_volume_sellers` (8) policies have **no**
`APPLIES_TO_CATEGORY` edges, and `category_policy_guide_paths` requires one in its
base `MATCH`. So they are structurally unreachable through that template, whatever
the filter says.

The keep-`all_sellers` branch in the seller predicate (§2.3) is therefore correct but
currently unreachable via this route. Those policies do reach the answer, through the
vector leg's `policy_seller_ids` payload added in Task 2.

Pinned by `test_policy_template_reaches_only_category_scoped_policies` so it cannot
change unnoticed. Widening the traversal would change the template's shape, which is
a D-4-style decision and was not taken unilaterally.

## 8.5 Frozen-defect tests that had to change

`tests/test_allowlist_frozen.py` asserted the defects M0 deliberately recorded
("M2 changes this; M0 records it"). Six assertions were updated:

| was | now |
|---|---|
| `("sql_intent", None)` rejected | null intents accepted; two more injection strings added in their place |
| `("graph_intent", None)` rejected | as above |
| `vector_artifact_groups=[]` rejected | legal when another leg is selected |
| `vector_artifact_groups=None` rejected | normalised to `[]` |
| `test_sql_only_plan_is_currently_unrepresentable` | `test_sql_only_plan_is_representable`, plus graph-only and vector-only |
| `test_templates_bind_only_limit` | `test_templates_bind_exactly_their_declared_parameters` |

Every injection-shaped case is retained and two more were added. Three new tests
close the gap the relaxation opens: an answerable plan with no legs is rejected, an
unanswerable one with no legs is accepted as a refusal, and template placeholders
must all be declared parameters.

## 8.6 Fixed en route: a `sys.path` leak that hid an import collision

`tests/test_repo_hygiene.py:277` did a bare `sys.path.insert(0, str(SRC))` and never
undid it. For the rest of the session `import observability` then resolved to the
empty package `src/observability/__init__.py` instead of
`src/observability/observability.py`, so importing `agentic_workflow` failed in the
full suite while passing on its own. Changed to `monkeypatch.syspath_prepend`, which
pytest restores. Latent since M1; surfaced because Task 3 is the first test to import
the workflow module.
