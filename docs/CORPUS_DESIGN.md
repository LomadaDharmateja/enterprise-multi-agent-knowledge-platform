# Synthetic corpus — design and determinism

Working document for milestone M1. Part 1 diagnoses why the current corpus does not
regenerate identically. Part 2 specifies the replacement corpus.

Nothing in Part 1 is fixed. It is a diagnosis, written before the rewrite so the rewrite
can be checked against it.

---

# Part 1 — Why the corpus does not regenerate identically

## Method

The stack was live, so every claim below is measured rather than read off the source. Two
full generator runs into separate directories, same seed, same database:

```
python src/synthetic/synthetic_data_generator.py --output-dir runA --report runA/report.json
python src/synthetic/synthetic_data_generator.py --output-dir runB --report runB/report.json
```

| artifact | records differing | of | differing, `created_at` ignored |
|---|---:|---:|---:|
| support_tickets | 2 | 3000 | **2** |
| logistics_incidents | 277 | 1000 | **277** |
| customer_emails | 2 | 3000 | **2** |
| warranty_claims | 0 | 1000 | 0 |
| policy_documents | 79 | 79 | 0 |
| troubleshooting_guides | 73 | 73 | 0 |

AUDIT.md P4 recorded 242 and 254 differing logistics records on its two runs; this run gave
277. **The count itself is not stable, which is the first clue** — this is a tie-break
lottery, not a fixed offset.

## The shape of the failure

The set of entities selected is stable. The mapping from artifact ID to entity is not.

```
logistics_incidents: entity SET vs entity->ID MAPPING
  order_id     set identical=True   records whose order_id changed under the same artifact id: 277
  seller_id    set identical=True   records whose seller_id changed under the same artifact id: 277
  product_id   set identical=True   records whose product_id changed under the same artifact id: 277
  customer_id  set identical=True   records whose customer_id changed under the same artifact id: 277
```

So `INC-000123` describes a different order on every regeneration. Neo4j nodes and Qdrant
points are keyed on those IDs. This is not "the corpus cannot be regenerated" — it is
**"regenerating it silently invalidates every saved report, evidence hash and frozen
baseline that cites an artifact ID."** That is the worse of the two failures, because it
produces no error.

## The causes, ranked by whether they are firing today

### N-1 — `ORDER BY` with no unique tiebreaker, logistics *(firing, dominant)*

`synthetic_data_generator.py:520`

```sql
ORDER BY os.delivery_delay_days DESC NULLS LAST
LIMIT :max_records;
```

A single non-unique sort column. Measured: **441 of the 1,000 selected rows share a
`delivery_delay_days` value with at least one other row.** PostgreSQL is free to return
tied rows in any order, and does:

```
run0: order_hash=4b634909fa5f2e10  set_hash=7669f02d7142ee1a
run1: order_hash=208590f6bedd2815  set_hash=7669f02d7142ee1a
run2: order_hash=e5a3d7dfffb977fe  set_hash=7669f02d7142ee1a
  ORDER stable across 3 runs: False
  SET   stable across 3 runs: True
```

Three runs, three orderings, one set. This alone accounts for all 277 differing logistics
records.

### N-2 — `ORDER BY` with no unique tiebreaker, support tickets *(firing, marginal)*

`synthetic_data_generator.py:417-421` sorts on four columns. Measured: **2 of 3,000 rows are
tied on the full four-column key**, and exactly 2 records differ. The four-column key is
*nearly* total, which is why this looks almost deterministic — and why it is more dangerous
than N-1. A defect that fires on 0.07% of records will not be noticed, and it is the same
defect.

### N-3 — `datetime.now()` in four places *(firing, cosmetic in content)*

`:735` and `:771` (policy documents), `:816` (troubleshooting guides), `:888` (report
`generated_at`). All 79 policies and all 73 guides differ on `created_at` and on nothing
else. Cosmetic in content, not in effect: those two files never hash equal, so a
content-addressed check over the corpus can never pass.

### N-4 — one global RNG stream indexed by position, not by entity *(firing; this is why N-1 is not cosmetic)*

`random.seed(seed)` at `:844` seeds one shared stream. Every generator consumes from it in
dataframe order, so each draw is bound to a **position**, not to a record.

That is why a reordering does not merely relabel rows. When two rows swap position they
also swap their `severity`, `status`, `channel`, `incident_type` and `created_at` offsets.
It is exactly the fingerprint in the diff — `title`, `summary`, `metadata`, `document_text`
and `linked_entities` all differing together on the same 277 records.

**And there is a cascade waiting.** The number of draws a record consumes is data-dependent,
because `determine_issue_type` (`:156`) and `determine_severity` (`:201`) return literals on
some branches and call `random.choice` on others. Today it happens to be constant:

```
support ticket       draws/record distribution: {6: 3000}   -> constant: True
logistics incident   draws/record distribution: {5: 1000}   -> constant: True
```

That constancy is an artefact of the degenerate slice — every ticket has `review_score = 1`,
so every ticket takes the same branch. Run the same measurement over the varied slice M1 is
required to produce and it stops being constant:

```
varied slice review_score distribution: {1: 333, 2: 95, 3: 267, 4: 582, 5: 1723}
draws/record on the varied slice: {5: 29, 6: 1065, 7: 1906}  -> constant: False
```

**The consequence is specific, and it constrains Part 2: the moment severity varies — which
Task 3 requires — a record consuming a different number of draws shifts the shared stream
for every record after it. A two-row reordering stops corrupting 2 records and starts
corrupting the entire tail of the file.** Fixing the `ORDER BY` alone would leave this in
place. The RNG has to be re-architected in the same change, not afterwards.

### N-5 — `DISTINCT ON` with a non-unique `ORDER BY` *(latent, not firing today)*

Both fetch queries pick one row per order with `DISTINCT ON`:

```sql
SELECT DISTINCT ON (oi.order_id) ... ORDER BY oi.order_id, oi.price DESC NULLS LAST   -- :354, :495
SELECT DISTINCT ON (r.order_id)  ... ORDER BY r.order_id, r.review_score ASC ...      -- :365
```

Measured ambiguity: **7,745 orders have two or more items tied on the top price**, and **126
orders have reviews tied on `(review_score, review_creation_date)`**. For those orders,
*which product and which seller the artifact is attributed to* is undefined by the query.

I tried to make it fire — four runs alternating `enable_seqscan` to force different plans —
and could not:

```
run0 vs run1: orders where chosen product_id differs = 0, seller_id differs = 0  (n=97276)
run0 vs run2: ... = 0, ... = 0
run0 vs run3: ... = 0, ... = 0
```

So this is an unexploded charge, not a current cause. It is worth more than its present
impact, because it is the one that would change **which seller a complaint is attributed
to** — the exact variable M1 exists to make meaningful. A PostgreSQL upgrade, an `ANALYZE`,
a new index or a parallel plan is enough to set it off, and the corpus would still
regenerate "successfully".

### N-6 — derivation by position propagates ticket instability *(firing, second-order)*

- `generate_customer_emails` (`:595`) iterates tickets in order and reuses
  `ticket["linked_entities"]` by reference. Ticket instability is copied verbatim — the same
  2 records differ in both files.
- `generate_warranty_claims` (`:641`) filters tickets by eligible issue type and then takes
  `eligible_tickets[:1000]`. Measured: **all 3,000 tickets are eligible, so 2,000 sit outside
  the slice.** Warranty claims came out identical across these two runs only because the two
  unstable tickets fell outside the prefix. On a less degenerate slice, prefix membership
  changes and warranty claims re-point too.

### N-7 — `numpy.random` is never seeded *(latent, currently unused)*

`numpy` is imported at `:12` and used only for `np.generic` type-checking at `:95`. No
`np.random` call exists, so it is not a current cause — but any statistical sampling added
in Part 2 will reach for it, and it is not covered by `random.seed(42)`.

### N-8 — `datetime.now()` fallback inside `choose_created_at` *(latent, never fires)*

`:131` falls back to wall-clock time when both `order_delivered_customer_date` and
`order_purchase_timestamp` are null. Measured on both candidate sets: `BOTH null = 0`. It has
never fired, and it would be invisible if it did.

### N-9 — serialisation *(not a cause, recorded for completeness)*

`write_jsonl` (`:108`) calls `json.dumps` with no `sort_keys`. Records are built as literal
dicts with fixed insertion order, so key order is stable today. It is a hazard only if a
record is ever assembled from an unordered source.

## Summary

| # | Cause | Site | Status | Scope |
|---|---|---|---|---|
| N-1 | `ORDER BY` not total (logistics) | `:520` | **firing** | 277 / 1000 |
| N-2 | `ORDER BY` not total (support) | `:417` | **firing** | 2 / 3000 |
| N-3 | `datetime.now()` in `created_at` | `:735,:771,:816,:888` | **firing** | 152 records |
| N-4 | positional global RNG, variable draw count | `:844,:156,:201` | **firing**; cascade latent | all, once severity varies |
| N-5 | `DISTINCT ON` tie | `:354,:365,:495` | latent | 7,745 orders at risk |
| N-6 | positional derivation (emails, warranty prefix) | `:595,:641` | propagation | inherits N-2 |
| N-7 | `numpy.random` unseeded | `:12` | latent | 0 today |
| N-8 | `choose_created_at` wall-clock fallback | `:131` | latent | 0 today |
| N-9 | `json.dumps` key order | `:108` | not a cause | 0 |

Two of these are not in AUDIT.md: **N-4's cascade** (the fix for zero variance is what arms
it) and **N-5** (`DISTINCT ON` ambiguity over 7,745 orders). One correction to the audit's
framing: it attributes the logistics instability to the `ORDER BY` alone and calls the fix
one line. The `ORDER BY` decides *that* rows move; N-4 decides that a moved row also changes
content. Both have to change.

## Incidental finding — severity has zero variance too

Not a determinism issue, but it belongs with the Part 2 requirements, and AUDIT.md does not
state it:

```
severity  : {'high': 3000}
issue_type: {'damaged_item': 730, 'missing_item': 733,
             'negative_review_escalation': 815, 'product_quality_complaint': 722}
```

The audit records `review_score = 1` and `is_late_delivery = True` on all 3,000 tickets.
**`severity` is also single-valued.** The code is reachable — `determine_severity` can return
`critical`, `high`, `medium` and `low` — but on a slice where every row has
`review_score = 1`, the `score <= 1` branch returns the literal `"high"` every time. Four
severity levels exist in the schema; one occurs in the data. `issue_type` is the only ticket
field carrying variance at all, and it varies over four near-synonymous complaint labels.
