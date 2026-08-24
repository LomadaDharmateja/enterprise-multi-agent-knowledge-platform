# Synthetic corpus — design and determinism

Working document for milestone M1. Part 1 diagnoses why the current corpus does not
regenerate identically. Part 2 specifies the replacement corpus. Part 3 records the
decisions taken at design review. Part 4 records what the implementation actually
measured.

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

---

# Part 2 — Design for the replacement corpus

**Status: proposal. No generation code is written. This part is for review.**

Everything below is validated against the live database with a throwaway prototype
(`scratchpad/proto_*.py`), so the numbers are measured on the real Olist distribution rather
than assumed. The prototype is not the implementation and will be discarded.

---

## 1. The design constraint, stated properly

The obvious reading of "make the corpus correlate with seller quality" produces a corpus
that fails for the same reason v1 failed.

I prototyped the direct approach first: score every seller by a quality deficit computed
from their Olist columns, allocate complaints in proportion, done. It yields

```
rho(complaints, quality_deficit) = 0.904
rho(complaints, n_orders)        = 0.472
```

`rho = 0.90` is **not** a success. If complaint count is a monotone deterministic function
of a deficit that PostgreSQL can compute, then PostgreSQL recovers complaint count exactly,
and the vector layer again contains nothing the SQL layer does not have. That is AUDIT.md
P1's finding — *"it is the SQL route with different formatting"* — reappearing through
`late_rate` instead of through `n_orders`. Trading one tautology for another is not progress.

So the 0.3–0.6 band is not a tuning target to be hit by fiddling. **It is the specification.**

- `rho ~ 0.0` (v1's null, +0.017): the corpus is noise; retrieval discovers nothing real.
- `rho ~ 0.87` (v1 actual, driven by volume): the corpus is a restatement of `count(orders)`.
- `rho ~ 0.90` (naive quality-driven): the corpus is a restatement of `avg(review_score)`.
- **`rho ~ 0.3–0.6`: correlated enough that the signal is real, decorrelated enough that a
  measurable part of it is not recoverable from any structured column.** That residual is
  the entire justification for having a vector layer.

Every design decision below follows from that one sentence.

## 2. Where the independent information comes from

The gap between `rho = 0.9` and `rho = 0.45` has to be filled by something. It cannot be
noise — noise is not information and would not survive the "signal the SQL layer does not
have" requirement either. It has to be a *real latent property that the Olist schema does
not record*.

That property is **operational support quality**: how a seller actually behaves once
something goes wrong. Response latency, willingness to replace rather than argue, packaging
discipline, whether a defect gets fixed or recurs. This is exactly the material the rebuild
plan already calls for — *"root causes, resolution paths, agent notes, policy exceptions,
escalation history"* — and none of it is in the Olist tables. A seller can have a 4.6 review
average because their product is good and still be an operational disaster when a return is
needed. That seller is invisible to SQL and visible in the corpus.

Implementation: a stable per-seller trait derived from the seller ID and the committed seed.

```
latent(seller_id) = sha256(SEED | "ops" | seller_id)[:8] / 2**64      -> [0, 1)
```

Deterministic and bit-reproducible, because it is a pure function of committed inputs.
Verified independent of every observable:

```
rho(latent, quality_deficit) = -0.0215
rho(latent, late_rate)       = -0.0305
rho(latent, mean_review)     = +0.0027
rho(latent, n_orders)        = -0.0126
```

> **This needs your explicit sign-off, because it is a reinterpretation of your brief.**
> You wrote: *"The linkage must be a function of the seller's actual Olist attributes …
> not a uniform random sample."* This design is a function of Olist attributes **plus** a
> per-seller trait that is not an Olist attribute. It is not a uniform random sample and it
> is not a per-ticket random draw — it is a fixed property of each seller, stable across
> runs. But the 0.3–0.6 band **cannot be reached without a component outside the structured
> schema**, because anything computed purely from Olist columns is by construction
> recoverable from Olist columns. If you want linkage that is *purely* a function of Olist
> attributes, the achievable correlation is ~0.9 and the corpus is tautological again. I
> think the trait is the right call and the band is the stronger requirement, but it is your
> call, not mine.

## 3. Seller attributes and the quality deficit

Computed once, from Olist only, with a total ordering on every tiebreak
(`scratchpad/seller_attrs.py`). 3,095 sellers; 3,030 have both orders and reviews.

| attribute | source | median | p75 |
|---|---|---:|---:|
| `n_orders` | distinct orders in `order_items` | 6 | 22 |
| `late_rate` | mean `is_late_delivery` over `vw_order_summary` | 0.000 | 0.095 |
| `mean_review` | mean `review_score` over the seller's orders | 4.19 | 4.62 |
| `bad_share` | share of reviews `<= 2` | — | — |
| `dominant_category` | modal `product_category_name`, ties broken by name | — | — |
| `category_share` | that category's share of the seller's items | 1.00 | 1.00 |

```
quality_deficit = pctrank( 0.40*pctrank(late_rate)
                         + 0.40*pctrank(-mean_review)
                         + 0.20*pctrank(bad_share) )
```

Percentile rank rather than z-score: `n_orders` is heavily skewed (mean 32, max 1,854) and
rank is robust to that without needing a transform. Weights are a judgement call — late
delivery and review score are the two things the flagship question actually asks about, and
`bad_share` is a partial duplicate of `mean_review`, so it gets less.

## 4. Complaint allocation

```
intensity_s   = pctrank( W * quality_deficit_s + (1-W) * latent_s )
weight_s      = intensity_s ** ALPHA  *  log1p(n_orders_s) ** BETA
complaints_s  = floor( 3000 * weight_s / sum(weight) ),  capped at n_orders_s
                + deterministic largest-remainder top-up, ties broken by seller_id
```

- `log1p(n_orders)` not `n_orders`: a seller with 100× the orders should have more
  complaints, but not 100× more. This sublinear term is what holds `rho(complaints,
  n_orders)` down at 0.374 instead of v1's 0.867.
- `ALPHA = 3.0` concentrates complaints on the worse tail rather than spreading them flat.
- Capped at `n_orders`: a seller cannot receive more complaints than they have orders.
- Largest-remainder top-up with a `seller_id` tiebreak so the total is exactly 3,000 and the
  allocation is reproducible.

**`W = 0.45`, `ALPHA = 3.0`, `BETA = 0.6`.** `W` is the dial; the sweep is in
`scratchpad/proto_alloc2.py` and moves `rho(complaints, late_rate)` smoothly from 0.20 at
`W=0.2` to 0.69 at `W=1.0`.

Which specific orders become complaints, within a seller: rank that seller's orders by an
order-level badness score (`review_score` ascending, `delivery_delay_days` descending), tie
broken by `order_id`. Deterministic, and it means the complaint lands on the seller's
genuinely worst orders rather than an arbitrary one.

## 5. Measured result of the proposed design

n = 3,030 sellers. Bootstrap CIs, 2,000 resamples.

| statistic | value | 95% CI | requirement | |
|---|---:|---|---|---|
| `rho(complaints, late_rate)` — **Task 4 headline** | **+0.425** | [+0.395, +0.455] | in 0.3–0.6 | PASS |
| `rho(complaints, mean_review)` — **Task 5 headline** | **−0.415** | [−0.444, −0.386] | magnitude in 0.3–0.6 | PASS |
| `rho(complaints, quality_deficit)` composite | +0.535 | [+0.510, +0.560] | in 0.3–0.6 | PASS |
| `rho(complaints, n_orders)` | +0.374 | [+0.343, +0.405] | far from +0.87 | PASS |

The CI on the headline excludes the audit's null of +0.017 by ~26 standard errors, and
excludes +0.87 by a similar margin. Both halves of the exit criterion are satisfied with
room, not marginally.

**The explicit top-vs-bottom quartile check** (sellers with `n_orders >= 5`; quartiles on
one-order sellers are meaningless):

| quartile by late_rate | sellers | mean complaints | median | % with ≥1 | mean late_rate | mean review |
|---|---:|---:|---:|---:|---:|---:|
| Q1 (best) | 440 | 0.641 | 0 | 41.1% | 0.000 | 4.267 |
| Q2 | 440 | 1.257 | 1 | 56.8% | 0.028 | 4.228 |
| Q3 | 440 | 1.675 | 1 | 63.4% | 0.086 | 4.105 |
| Q4 (worst) | 440 | 1.845 | 2 | 76.4% | 0.196 | 3.874 |

Top quartile carries **2.88× the complaints** of the bottom quartile, monotone across all
four, Mann-Whitney U p = 3.6e-37.

**The qualitative requirements, checked rather than asserted:**

| requirement | measured |
|---|---|
| complaints must land on well-rated sellers | 774 sellers with `mean_review >= 4.0` carry ≥1 complaint — **47.0% of the corpus** |
| high-volume well-rated sellers with few complaints | 116 of 303 top-decile-volume sellers are well-rated with ≤1 complaint |
| a bad seller need not have complaints | 141 of 757 worst-quartile-by-deficit sellers have **zero** |
| severity must vary | see §6; no field is single-valued |

Complaint distribution: `{0: 1528, 1: 719, 2: 354, 3: 228, 4: 132, 5: 54, 6: 14, 7: 1}` —
1,502 sellers carry at least one, versus 860 in v1.

## 6. Per-artifact design

Each type answers your three questions. "Deterministic" below means: a pure function of
committed inputs (Olist attributes, the seed, and the per-seller trait), with a total
ordering on every selection and tiebreak — no positional RNG, no unstable `ORDER BY`.

### support_tickets — 3,000

| | |
|---|---|
| **What SQL/graph cannot return** | *Why* the order went wrong (root-cause taxonomy), what the agent did about it (resolution path), whether it escalated and to what tier, how many contacts it took, and the agent's free-text note. SQL has `review_score` and `is_late_delivery`; it has no column for cause, remedy, or escalation. |
| **Variation** | `severity` ∈ {low, medium, high, critical} driven by delay magnitude, review score and the seller's latent trait — **not single-valued as today**. `sentiment` ∈ {frustrated, neutral, resigned, angry, satisfied-after-resolution}, deliberately not collinear with `review_score`. `resolution` ∈ {refund, replacement, partial refund, explained-no-action, unresolved, withdrawn}. `escalation_tier` ∈ {none, tier-2, ops-review, seller-account-review}. `contact_count` 1–5. `days_to_resolution`, null when unresolved. |
| **Linkage** | Seller via §4 allocation; order = that seller's *k*-th worst order by (review_score asc, delay desc, order_id); customer/product from that order. Deterministic. |

The sentiment/severity split is where the independent signal lives: a `critical` ticket with
resolution `replacement` and `days_to_resolution = 2` describes a good seller having a bad
day, while `medium` + `unresolved` + `contact_count = 4` describes a seller who does not
answer. Those two are indistinguishable in SQL and obvious in the text.

### customer_emails — ~1,200 (not 3,000)

| | |
|---|---|
| **What SQL/graph cannot return** | Thread structure over time — a second and third contact, tone hardening between messages, "still nothing after 11 days". No table has a conversation. |
| **Variation** | 1–3 messages per thread; tone escalating with `contact_count`; some threads close politely, some go silent. |
| **Linkage** | Deterministic subset of tickets: only those with `resolution ∈ {unresolved, partial}` or `escalation_tier != none`. |

**This is a deliberate departure from v1**, where emails were a 1:1 clone of tickets reusing
`linked_entities` *by reference* — AUDIT.md counts the true entity information as 3,000 +
1,000, not 8,152. A clone cannot corroborate its original. Making email coverage a *biased
subset* means "which sellers generated follow-up threads" is a genuinely different question
from "which sellers have tickets", and the answer is not in SQL.

### logistics_incidents — 1,000

| | |
|---|---|
| **What SQL/graph cannot return** | *Why* a shipment was late: carrier backlog, weather, customs hold, address error, hub mis-sort, seller dispatch delay. `vw_order_summary` has `delivery_delay_days` and no cause column. |
| **Variation** | `root_cause` across those six; `fault_party` ∈ {carrier, seller, customer, force-majeure}; `corrective_action`; `recurrence_flag`. |
| **Linkage** | Order selected by `delivery_delay_days` desc **with `order_id` as final tiebreak** (fixes N-1); seller/product/customer from that order. |

**`fault_party` is the most valuable field in the corpus.** A seller with a high `late_rate`
whose incidents are all `fault_party = carrier` is being punished by SQL for something that
is not their fault. That directly changes the answer to "which sellers are underperforming",
it is not recoverable from any structured column, and it is exactly what a hybrid system
should be able to surface and a SQL-only system cannot.

### warranty_claims — 1,000

| | |
|---|---|
| **What SQL/graph cannot return** | Defect mode, whether the claim was honoured, and whether the same defect recurs on the same product. Olist has no returns or warranty data at all. |
| **Variation** | `defect_mode`; `claim_outcome` ∈ {approved, rejected, pending, withdrawn}; `under_warranty` bool; `repeat_defect` bool; `resolution_days`. |
| **Linkage** | Allocated over (seller, product) pairs by a defect-propensity score = category base rate × seller latent trait — **not a prefix slice of tickets** (fixes N-6). Independent of the complaint allocation, so a seller may have warranty claims without tickets and vice versa. |

The rebuild plan requires *"some well-rated sellers must have unresolved warranty issues"*.
Decoupling this allocation from the ticket allocation is what makes that possible; in v1
warranty claims were `tickets[:1000]`, so they could not disagree with tickets by
construction.

### policy_documents — ~24 distinct (not 79 near-duplicates)

| | |
|---|---|
| **What SQL/graph cannot return** | The rule that governs a case, its exceptions, and its scope of applicability. There is no policy table. |
| **Variation** | 8 topics × 3 applicability scopes (all-sellers / category-scoped / high-volume-seller-scoped), each with a genuinely distinct body, an exceptions clause, and an effective date. |
| **Linkage** | **Category-scoped policies link to every seller whose `dominant_category` is in scope** (§7). Deterministic. |

Fixes F-15: v1 emitted 73 policies that were one sentence with the category name swapped —
near-duplicates in a 384-dim cosine space, so top-5 retrieval returned five interchangeable
paragraphs. 24 distinct documents retrieve better than 79 identical ones.

### troubleshooting_guides — 73 (one per category), rewritten

| | |
|---|---|
| **What SQL/graph cannot return** | The diagnostic procedure for a defect class. |
| **Variation** | Per-category symptom lists and step sequences keyed to the defect modes that actually occur in that category. |
| **Linkage** | Category → sellers with that `dominant_category`, same mechanism as policies. |

## 7. Policy → seller linkage, which you asked me to check

**The relationship exists in the structured data and is simply not materialised into the
corpus.** Measured (`scratchpad/diag_seller_cat.py`):

```
seller x category pairs          : 6359
sellers with at least one category: 3035 of 3095
categories per seller            : median 1, mean 2.10, max 27
sellers selling exactly 1 category: 1728 (56.9%)
dominant-category share          : median 1.00
  sellers whose top category is >=50% of their items: 93.3%
  ...                             >=80%:              71.5%
categories: 73, sellers per category: median 36, min 1 (cds_dvds_musicais), max 492 (beleza_saude)
```

So `seller -> order_items -> products -> product_category_name` gives every seller a
well-defined dominant category for 93.3% of sellers at a ≥50% share, and `dominant_category`
is a legitimate description of the seller rather than an artefact of a thin tail. A policy
about electronics can point at sellers who actually sell electronics.

Two cautions the implementation must handle:

1. **60 sellers have no category at all** (no order items). They get no category-scoped
   policy. They must be *excluded explicitly*, not silently defaulted — that is the F-06
   failure mode.
2. **`product_category_name_english` is NULL for all 73 rows** (F-04). Policy titles would
   read "Category Support Policy: None". **M1 depends on the F-04 fix**: `FILE_CANDIDATES`
   in `postgres_loader.py` does not list `translations_cleaned.csv` and the glob fallback
   misses it. This must be fixed before generation or the corpus bakes in 73 NULL category
   names. Flagging it as a prerequisite, not folding it in silently.

## 8. Determinism architecture

Point-by-point against Part 1.

| cause | fix |
|---|---|
| N-1, N-2 | Every `ORDER BY` ends in a unique column (`order_id`, `seller_id`). Asserted in code: the generator verifies the selection key is unique before use, and raises if not. |
| N-3 | No `datetime.now()` anywhere in generation. `created_at` derives from the order's own timestamps plus a deterministic per-record offset. The report's `generated_at` becomes the corpus content hash. |
| N-4 | **The single global RNG stream is removed.** Every record draws from `Random(sha256(SEED \| artifact_type \| entity_id))` — an RNG seeded per record, from the entity it describes. A record's fields then depend only on its own identity, so reordering cannot change content, and a branch consuming a different number of draws cannot desynchronise anything. This is what makes varying severity safe. |
| N-5 | `DISTINCT ON` gets a total `ORDER BY` (`..., oi.order_item_id` / `..., r.review_id`). Defuses the 7,745-order ambiguity before it fires. |
| N-6 | Emails and warranty claims are allocated from entity attributes, not from a positional slice of the ticket list. |
| N-7 | No `numpy.random`. Where vectorised work is needed, `numpy.random.default_rng(seed)` explicitly — never the global. |
| N-8 | The wall-clock fallback is deleted; a record with no usable timestamp is an error, not a silent guess. |
| N-9 | `json.dumps(..., sort_keys=True)` so key order cannot drift. |

Seed: committed constant `SEED = "enterprise-ai-m1-v1"` plus the existing `--seed` integer.
Verification is `sha256` per file across two clean runs, diffing to zero — as Task 4 requires.

## 9. Text decisions

### Language: generate English messages. Keep the Portuguese as a separate quoted field.

`customer_message` becomes generated English. The original Portuguese review text is retained
verbatim in `source_review_excerpt`, which is **not embedded**, so provenance survives without
an English-only MiniLM embedding Portuguese.

Reasons, in order: (a) the embedding model is `all-MiniLM-L6-v2`, English-only, and 100% of
v1 messages were Portuguese — the retrieval leg was embedding text the model cannot represent;
(b) the fields carrying the new independent signal (root cause, resolution, escalation) are
authored, not quoted, so they would be English regardless, and a half-Portuguese record embeds
worse than either pure option; (c) AUDIT.md #92 notes that calling verbatim customer-authored
review text "synthetic" is a privacy-adjacent overclaim — generated text removes the problem
rather than caveating it. Switching to a multilingual model was the alternative; it would
change every stored vector and invalidate the M0 baseline for a benefit M1 does not need.

### **Correction: there is no double-encoded UTF-8 to fix.**

Your Task 4 asks me to fix double-encoded UTF-8 in customer messages. I could not reproduce
it, and I believe the finding is a false positive.

AUDIT.md F-16 reports `tickets containing mojibake 'Ã': 70`, giving `"atÃ©"` for `"até"` as
the example. That count comes from searching for the bare character `Ã`. I reproduced the 70
exactly, then looked at what the matches are:

```
tickets containing the character 'A-tilde' at all : 70   <- what F-16 counted
tickets with GENUINE double-encoding              : 0

'A-tilde' + next char, by frequency:
   'ÃO'  x110   -> LEGITIMATE  (uppercase "NÃO" / "SÃO")
   'Ão'  x2     -> the reviewer's own typo
```

110 of the 112 occurrences are the correctly-encoded uppercase of `não`/`são`. The remaining
two are inside `"a segunda nÃo recebi justificativa"` and `"NÃo recomendo a ninguém"` — human
typos in the original review, and note `ninguém` renders correctly in the same string, which
is the proof the encoding is clean. A `latin-1 -> utf-8` repair fails on 70 of 70, as it must,
because there is nothing double-encoded to repair.

So: no encoding fix is needed, and switching to English is a signal-quality decision on its
own merits, not a workaround. **If you have a specific record that renders wrongly, send it
and I will re-open this** — I would rather be corrected than have this one wrong.

### Truncation: the documents are not too long; ingest inflates them.

F-10's 9.4% reproduces exactly — and it is not a document-length problem.

```
artifact                     n  median   max       over 256   string measured
support_tickets           3000     182   252      0 ( 0.0%)   generator 'document_text' field
                          3000     176   258      1 ( 0.0%)   ingest build_document_text()  <- embedded
customer_emails           3000     139   211      0 ( 0.0%)   generator 'document_text' field
                          3000     212   352    708 (23.6%)   ingest build_document_text()  <- embedded
troubleshooting_guides      73     120   152      0 ( 0.0%)   generator 'document_text' field
                            73     262   326     54 (74.0%)   ingest build_document_text()  <- embedded

TOTAL truncated at ingest: 763/8152 (9.4%)   AUDIT.md F-10: 763/8152 (9.4%)
```

**The generator's own `document_text` never exceeds 256 word-pieces — troubleshooting guides
peak at 152, less than 60% of the window.** All 763 truncations are created by
`qdrant_ingest.build_document_text()` discarding the authored field and rebuilding the text,
which inflates guides from a median of 120 tokens to 262.

That is the same root cause as F-01. Ingest ignores the generator's `document_text` and
rebuilds from top-level record keys — which is why the entity IDs vanish (F-01) *and* why the
text overflows (F-10). One fix addresses both: make the generator's `document_text`
authoritative and have ingest embed it as-is.

**Decision: keep the documents short and make the authored field authoritative — no chunking,
no raised limit.** Chunking 8,152 documents that already fit, or swapping to a longer-context
model, would both be treating a symptom. M1's side is to author `document_text` as the single
embeddable string and add a generation-time assertion that every record tokenises under 256
word-pieces with margin, so this cannot regress silently.

**Scope boundary:** the ingest half of this is F-01/F-02, which the rebuild plan assigns to
**M2**. M1 makes the field correct and asserts it fits; M1 alone will not make the 9.4% go to
zero in Qdrant, because ingest still rebuilds the text. I am not folding M2's fix into M1
silently — but the truncation figure will not improve until M2 lands, and I would rather say
that now than report a fixed number that is not fixed.

## 10. Loader strictness (F-06)

`standardize_columns` currently does:

```python
for column in expected_columns:
    if column not in df.columns:
        df[column] = None          # no log, no warning, no failure
```

This is the delivery mechanism for F-04 and F-05. Replacement: compare the expected column
set against what the file provides and `raise` on any missing column, naming the file, the
missing columns and the columns actually present. Explicit renames stay in `COLUMN_RENAMES`
(`sentiment` -> `sentiment_label` is a genuine rename and should be declared as one, not
silently invented as NULL). No column is ever created implicitly.

## 11. Open questions for you

1. **The latent trait (§2).** The 0.3–0.6 band is unreachable using only Olist columns. I
   have added a deterministic per-seller trait outside the schema. Confirm, or tell me to
   stay purely within Olist attributes and accept `rho ~ 0.9`.
2. **Which correlation is *the* number, and its sign.** Task 4 names late-delivery rate
   (positive, +0.425). Task 5 names review score (negative, −0.415). "In the range 0.3–0.6"
   cannot be literally true of −0.415. I propose: the CI test asserts on
   `|rho(complaints, mean_review)|` and the reported headline is
   `rho(complaints, late_rate)`, with all three printed. Say if you want it otherwise.
3. **CI sample size.** Task 5 says "take a sample of sellers". Measured flake rate over 300
   simulated samples:

   | sample | mean rho | 5th pct | 95th pct | P(in 0.3–0.6) |
   |---:|---:|---:|---:|---:|
   | 100 | +0.411 | +0.260 | +0.558 | **0.87** |
   | 200 | +0.418 | +0.308 | +0.512 | 0.97 |
   | 400 | +0.424 | +0.354 | +0.487 | **1.00** |

   A 100-seller sample fails **13% of CI runs** on a corpus that is correct. I propose a
   fixed seeded sample of 400, or the full population. Either is deterministic; n=100 is not.
4. **Artifact counts.** Emails drop 3,000 → ~1,200 and policies 79 → ~24, on the reasoning in
   §6. Total goes from 8,152 to roughly 6,400. The headline number gets smaller and the
   information content goes up. Confirm you are happy losing the bigger number.
5. **F-04 is a prerequisite** (§7). The translations loader fix has to land before generation
   or 73 NULL category names are baked into the corpus. It is M1 scope by the rebuild plan's
   loader bullet; I want it confirmed as in-scope before I touch `postgres_loader.py`.

**Stopping here for review, as instructed. No generation code will be written until you
confirm.**

---

# Part 3 — Decisions confirmed at review

Part 2 was approved on 2026-08-24. The five open questions are settled as follows, and
this section is the record of what was decided and why.

## D-1 — The latent operational trait is confirmed

Approved as designed. `rho ~ 0.9` restates a PostgreSQL column the same way `rho ~ 0.87`
did. The 0.3–0.6 band stands as the specification, not as a tuning preference.

## D-2 — Sign convention

**`rho(complaint_count, late_delivery_rate)` is the headline statistic**, and the CI test
asserts `abs(rho)` falls in `[0.3, 0.6]`.

Recorded for completeness, from the design prototype: the corresponding correlation against
seller review score is **−0.415** (95% CI [−0.444, −0.386]). The sign is negative because
`mean_review` is a quality measure while `late_rate` is a defect measure — more complaints
means a worse seller on both, which reads as a positive correlation with lateness and a
negative one with review score. Both describe the same relationship. Asserting on `abs(rho)`
lets the test accept either framing without the sign convention becoming a silent trap.

## D-3 — CI sample size is 400, and that is a measurement

**Not a magic number.** It comes from simulating the CI test 300 times at each candidate
sample size against a corpus known to be correct, and recording how often it lands in the
0.3–0.6 band:

| sample size | mean rho | 5th pct | 95th pct | P(in 0.3–0.6) | verdict |
|---:|---:|---:|---:|---:|---|
| 100 | +0.411 | +0.260 | +0.558 | **0.87** | fails 13% of runs on correct data |
| 200 | +0.418 | +0.308 | +0.512 | 0.97 | fails 3% of runs |
| 400 | +0.424 | +0.354 | +0.487 | **1.00** | 0 failures in 300 trials |

A test that fails 13% of the time on correct data is worse than no test: it trains everyone
to re-run CI until it goes green, which is precisely how a real regression gets waved
through. At n=400 the sampling interval [+0.354, +0.487] sits comfortably inside the band on
both sides, so the test fails when the corpus is wrong rather than when the sampler is
unlucky.

The sample is drawn with a **committed seed**, so the same 400 sellers are selected on every
run and the test is deterministic rather than merely usually-passing.

## D-4 — Artifact count drops to ~6,400, intentionally

Approved. The corpus goes from 8,152 records to roughly 6,400, and **this reduction is
deliberate, not attrition.** Recording it here so nobody later reads the smaller number as a
regression:

| artifact | v1 | M1 | why |
|---|---:|---:|---|
| support_tickets | 3,000 | 3,000 | unchanged |
| customer_emails | 3,000 | ~1,200 | v1 was a 1:1 clone of tickets reusing `linked_entities` **by reference**; a clone cannot corroborate its original |
| logistics_incidents | 1,000 | 1,000 | unchanged |
| warranty_claims | 1,000 | 1,000 | no longer a prefix slice of tickets |
| policy_documents | 79 | ~24 | F-15: 73 of the 79 were one sentence with the category name swapped — near-duplicates in cosine space |
| troubleshooting_guides | 73 | 73 | unchanged in count, rewritten in content |

AUDIT.md's own accounting is the argument: of "8,152 records", the true *entity* information
content was 3,000 + 1,000, because emails duplicated tickets exactly and warranty claims
were a subset. The record count fell by 21%; the number of distinct entity–artifact
relationships went up. **Documentation must be updated to the real number when M1 completes
— 8,152 is retired and must not be quoted again.**

## D-5 — F-04 is M1 scope and is fixed

Confirmed as M1 scope and completed before any generation ran (commit `6f23ce6`). Details in
§10 and in the commit; the summary is that `standardize_columns` no longer invents columns,
and making it strict immediately surfaced a third instance of the same defect —
`orders.shipment_status`, NULL for all 99,441 rows.

## D-6 — Truncation stays at 9.4% until M2, and here is the handoff

**Requested explicitly at review, so that M2 can pick this up without re-diagnosing it.**

**The document truncation rate will remain at 763 of 8,152 documents — 9.4% — after M1
completes, and M1 cannot fix it.** The cause is not in the generator. It is in
`src/vector/qdrant_ingest.py`, in `build_document_text()` (`qdrant_ingest.py:128`), which
discards the generator's authored `document_text` field and rebuilds the embedded string
from top-level record keys. The rebuild inflates the text past the 256 word-piece window of
`all-MiniLM-L6-v2`.

Measured, per artifact group, comparing the two candidate strings:

| artifact group | authored `document_text` over 256 | ingest-rebuilt text over 256 |
|---|---:|---:|
| support_tickets | 0 / 3000 (0.0%) | 1 / 3000 (0.0%) |
| customer_emails | 0 / 3000 (0.0%) | **708 / 3000 (23.6%)** |
| logistics_incidents | 0 / 1000 (0.0%) | 0 / 1000 (0.0%) |
| warranty_claims | 0 / 1000 (0.0%) | 0 / 1000 (0.0%) |
| policy_documents | 0 / 79 (0.0%) | 0 / 79 (0.0%) |
| troubleshooting_guides | 0 / 73 (0.0%) | **54 / 73 (74.0%)** |
| **total** | **0 / 8152 (0.0%)** | **763 / 8152 (9.4%)** |

Troubleshooting guides go from a median of 120 word-pieces as authored to 262 as rebuilt;
customer emails from 139 to 212. **The authored text never exceeds the window — guides peak
at 152 word-pieces, under 60% of the limit.** Every one of the 763 truncations is created by
the rebuild.

This is the same root cause as **F-01**: ingest reads top-level record keys and ignores the
generator's own `document_text`, which is why entity IDs are lost *and* why the text
overflows. The rebuild plan assigns F-01 and F-02 to **M2**, so the fix belongs there.

**Handoff to M2 — one change closes F-01, F-02 and F-10 together:** have `build_payload()`
and the ingest path embed the generator's `document_text` verbatim instead of calling
`build_document_text()`, and write it to the payload key the retriever actually reads
(`text_preview`, per F-02). M1's contribution is to make that field authoritative and to
assert at generation time that every record tokenises under 256 word-pieces with margin, so
the guarantee is already in place when M2 stops rebuilding. Expected result after M2:
truncation 0.0%.

Until then, any report quoting truncation must say 9.4%, not zero.

---

# Part 4 — Implementation results (M1 tasks 4 and 5)

Measured on the corpus in `data/synthetic`, corpus hash
`3a70b8ddb3da0a1ee45dc503897a99b2893e88f6cce759f2bf4b210fe75b5310`.

## Determinism: bit-identical

Two consecutive runs into separate directories, same database:

```
IDENTICAL  support_tickets          IDENTICAL  warranty_claims
IDENTICAL  logistics_incidents      IDENTICAL  policy_documents
IDENTICAL  customer_emails          IDENTICAL  troubleshooting_guides
REPORTS IDENTICAL
```

All nine causes from Part 1 are addressed. The report no longer carries a
`generated_at` wall-clock field; a SHA-256 `corpus_hash` over the six file hashes
identifies the run instead, so two runs of the same corpus produce the same report.

## Correlations on the generated corpus

The design prototype predicted +0.425 from the pre-fix database. Re-measured on the
real corpus after the loader reload rebuilt `vw_order_summary`:

| statistic | prototype | **generated corpus** | 95% CI | requirement | |
|---|---:|---:|---|---|---|
| `rho(complaints, late_rate)` — headline | +0.425 | **+0.427** | [+0.397, +0.456] | in 0.3–0.6 | PASS |
| `rho(complaints, mean_review)` | −0.415 | **−0.412** | [−0.440, −0.383] | \|rho\| in 0.3–0.6 | PASS |
| `rho(complaints, n_orders)` | +0.374 | **+0.376** | [+0.345, +0.407] | far from +0.867 | PASS |

The loader reload did not move the correlations materially — the columns it repaired
(`sentiment_label`, `shipment_status`, `product_category_name_english`) do not feed the
allocation. **`DEFICIT_WEIGHT` was left at 0.45; no adjustment was needed.**

Top-vs-bottom quartile by late-delivery rate (`n_orders >= 5`):

| quartile | sellers | mean complaints | median | % with ≥1 | mean late_rate | mean review |
|---|---:|---:|---:|---:|---:|---:|
| Q1 (best) | 440 | 0.645 | 0 | 41.4% | 0.000 | 4.267 |
| Q2 | 440 | 1.259 | 1 | 56.8% | 0.028 | 4.228 |
| Q3 | 440 | 1.677 | 1 | 63.4% | 0.086 | 4.105 |
| Q4 (worst) | 440 | 1.848 | 2 | 76.4% | 0.196 | 3.874 |

**2.86×**, monotone across all four, Mann-Whitney U p = 5.99e-37.

## Field variance — the zero-variance problem is gone

v1: `severity = 'high'` on all 3,000 tickets, `review_score = 1` on all 3,000,
`is_late_delivery = True` on all 3,000.

```
severity     high 1058, medium 1056, critical 447, low 439
sentiment    resigned 883, frustrated 729, satisfied_after_resolution 528,
             neutral 476, angry 384
resolution   unresolved 886, refund_issued 550, replacement_shipped 492,
             partial_refund 426, explained_no_action 403, withdrawn 243
escalation   none 1458, tier_2 1105, ops_review 267, seller_account_review 170
```

47.1% of complaints land on sellers rated ≥ 4.0; 116 of the 303 top-decile-volume
sellers are well rated with ≤ 1 complaint. A complaint no longer implies a bad seller.

### Threshold calibration — a correction to the first implementation

The first working generator produced `critical` on 55% of tickets and `unresolved` on
71%. That is variance, but not *realistic* variance: "critical" has to be the rare tail
or the field carries no ranking signal, and a 71% unresolved rate made the email subset a
near-clone of the ticket set again — the exact v1 defect the split exists to remove.

The thresholds were re-derived from the measured score distributions rather than guessed:
`SEVERITY_MEDIUM/HIGH/CRITICAL_THRESHOLD` are the p15/p50/p85 of the severity score, and
`RESOLUTION_MIXED/UNRESOLVED_THRESHOLD` the p65/p80 of the resolution pressure. The
constants are committed with that provenance recorded next to them.

## Final artifact counts

| artifact | v1 | design estimate | **actual** |
|---|---:|---:|---:|
| support_tickets | 3,000 | 3,000 | **3,000** |
| logistics_incidents | 1,000 | 1,000 | **1,000** |
| customer_emails | 3,000 | ~1,200 | **985** |
| warranty_claims | 1,000 | 1,000 | **1,000** |
| policy_documents | 79 | ~24 | **40** |
| troubleshooting_guides | 73 | 73 | **73** |
| **total** | **8,152** | ~6,400 | **6,098** |

Two departures from the design estimate, both deliberate:

- **Policies are 40, not ~24.** The design said 8 topics × 3 scopes; the category-scoped
  scope expands to 3 category groups, so it is 8 × (1 all-sellers + 3 category groups +
  1 high-volume) = 40. All 40 have distinct bodies, against v1's 73 near-duplicates.
- **Emails are 985, not ~1,200.** The eligibility rule was tightened after measurement:
  the first rule matched 2,745 of 3,000 tickets, which recreated the v1 clone problem. A
  follow-up thread now requires the case to have actually dragged — unresolved, escalated
  to ops or seller review, or four contacts.

**8,152 is retired. The corpus is 6,098 records and the reduction is intentional.**

## Two defects found while wiring the corpus into Neo4j

Neither is in AUDIT.md; both were found by loading the new corpus.

1. **`synthetic_neo4j_loader.sanitize_value` could not handle list values.** It called
   `pd.isna(value)` before the `isinstance(value, (dict, list))` branch, and `pd.isna()`
   on a list returns an elementwise array — so `if pd.isna(value)` raised *"truth value of
   an empty array is ambiguous"* and the JSON-encoding branch was unreachable for exactly
   the values it was written for. Reordered.
2. **`CustomerEmail -> SupportTicket` edges required a `ticket_id` key** the email records
   did not expose (they had `related_ticket_id`). `RELATED_TO_TICKET` was 0.

## Graph linkage now materialised

```
RELATED_TO_TICKET   985     (was 0 -- emails now expose ticket_id)
CLAIM_FOR_TICKET     79     (warranty/ticket order overlap; see below)
APPLIES_TO_CATEGORY 264
APPLIES_TO_SELLER 15,072    (did not exist in v1)
```

**`APPLIES_TO_SELLER` is the linkage AUDIT.md F-15 said was absent** — *"policies link to
no seller… the flagship question asks for sellers associated with relevant support
policies; that association does not exist in the data."* It exists now, driven by the
categories each seller actually sells.

`CLAIM_FOR_TICKET` at 79 is low **by design, not by defect.** v1's warranty claims were
`tickets[:1000]` — a prefix slice, so every claim had a ticket by construction and the two
could never disagree. They are now allocated independently, and 79 is the genuine overlap
where a warranty claim and a complaint happen to land on the same order. That
independence is what allows a well-rated seller to carry an unresolved warranty issue with
no complaint against it.

An "all sellers" policy stores its selection *rule* rather than enumerating 3,030 IDs:
enumerating them added ~800 KB to the corpus and 24k graph edges that discriminate
nothing. Category-scoped and high-volume policies carry their explicit seller sets.

## Qdrant: entity IDs now survive ingest

```
points: 6098
points carrying seller_id / order_id / product_id / customer_id: 5985 of 6098
support_ticket points with seller_id: 3000 of 3000
```

**v1 was 0 of 8,152 (F-01).** The 113 points without entity IDs are the 40 policies and 73
guides, which reference a category rather than a single order — correct, not a gap.

This is the *producer's* half of F-01: `qdrant_ingest.build_payload()` reads entity keys
from the top level of each record, so the generator now writes them there as well as in
`linked_entities`. **The consumer's half — ingest reading `linked_entities` and embedding
the authored `document_text` instead of rebuilding it — remains M2**, and until it lands
the truncation figure stays at the 9.4% recorded in D-6.

## The boundary test

`tests/test_corpus_signal.py`, four tests, in CI. It reads complaint counts from **Qdrant
payload metadata** and seller quality from **PostgreSQL**; nothing in it reads the
generator's output, so it fails if the JSONL → Qdrant handoff loses the linkage. That is
the direct answer to F-12, where twelve validators passed while the retrieval layer was
inert because none of them looked at a seam.

Sample: 400 sellers, seed `20260824`, committed. Assertion:
`0.3 <= abs(rho(complaints, late_delivery_rate)) <= 0.6`.

**The test was verified to fail on corpora that are wrong**, which matters more than that
it passes on this one:

| corpus | rho | test |
|---|---:|---|
| **actual (sampled n=400)** | **+0.380** | **PASSES** |
| seller linkage shuffled (v1's uniform null) | +0.021 | FAILS the band |
| complaints ∝ order count (v1's actual defect) | — | FAILS the volume test at rho=1.00 |
| complaints a pure function of `late_rate` | +0.9998 | FAILS the band |

The sampled +0.380 against the population +0.427 is ordinary sampling variation, and sits
inside the band as D-3's sizing predicted.
