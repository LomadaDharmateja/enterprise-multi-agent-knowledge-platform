# M2 — what changed, and why

Milestone M2 fixed the three defects that voided the project's architectural thesis:
**F-01** (entity IDs dropped at Qdrant ingest), **F-02** (the retriever read a payload
key ingest never wrote), and **F-03** (SQL and Cypher templates invariant to the
question). Nothing else was built: no new features, no reliability work, no caching.

Design document: `docs/M2_RETRIEVAL_DESIGN.md`. Baseline comparison data:
`tests/baseline/m2_baseline_comparison.json`.

---

## 1. The exit criterion, item by item

| criterion | result |
|---|---|
| two different questions provably return different evidence | **10 distinct SQL evidence sets from 10 questions** (M0: 5, with two pairs byte-identical). The only repeated graph hash is the hash of `[]`, shared by the two questions whose graph leg is correctly not selected. |
| a vector hit joins to a SQL row by entity ID in an automated test | `tests/test_entity_id_join.py::test_vector_hit_joins_to_sql_row` — **PASSES**. Runs a real vector query, takes `seller_id` to `vw_seller_performance` and `order_id` to `ecommerce.orders`, and asserts the two databases agree on `customer_id`. |
| the control returns different evidence or an explicit refusal | **Explicit refusal.** `answerable: false`, `overall_status: REFUSED`, no database queried. |
| the revenue question no longer crashes | **Completed.** SQL-only plan, `graph_intent: null`, 10 records, grounding 5. |

Full suite: **162 tests passing.**

---

## 2. Baseline comparison — all ten questions

Re-run with `scripts/compare_to_baseline.py` against the M0 freeze in
`tests/baseline/baseline_results.json`.

| question | M0 | M2 | sql/graph/vec records | answer chars | grounding |
|---|---|---|---|---|---|
| `seller_complaint_warranty_policy` | completed | completed | 10 / 10 / 15 | 3241 | 5 |
| `late_delivery_logistics_region_guidance` | completed | completed | 5 / 10 / 10 | 3340 | 5 |
| `payment_refund_policy` | completed | completed | 5 / 5 / 10 | 2534 | 5 |
| `product_quality_troubleshooting` | completed | completed | 3 / 5 / 10 | 2728 | 5 |
| `seller_negative_ticket_paths` | completed | completed | 2 / 10 / 5 | 3065 | 5 |
| `paraphrase_most_complaints` | completed | completed | 5 / 10 / 5 | 2662 | 5 |
| `paraphrase_vendors_poor_experience` | completed | completed | 2 / 10 / 5 | 2849 | 5 |
| `paraphrase_suppliers_negative_reviews` | completed | completed | 2 / 3 / 10 | 3055 | 5 (completeness 4) |
| `control_purple_monkey_dishwasher` | **crashed** | **refused** | 0 / 0 / 0 | — | — |
| `control_highest_revenue_seller` | **crashed** | **completed** | 10 / 0 / 0 | 1431 | 5 |

### What changed, and why

**Evidence changed on every question — measured against M0's stored hashes, not
asserted.** M0 recorded a `records_sha256` per leg per question, so this is a direct
diff:

| question | SQL evidence | graph evidence | M0 sql hash -> M2 sql hash |
|---|---|---|---|
| `seller_complaint_warranty_policy` | CHANGED | CHANGED | `fdd0e02b6d6b` -> `df86cc73f0d3` |
| `late_delivery_logistics_region_guidance` | CHANGED | CHANGED | `ea2e8c26d603` -> `02238669aa28` |
| `payment_refund_policy` | CHANGED | CHANGED | `73e6a358ee26` -> `d09978bc35da` |
| `product_quality_troubleshooting` | CHANGED | CHANGED | `9afa3a251568` -> `ca9d810b3110` |
| `seller_negative_ticket_paths` | CHANGED | CHANGED | `fdd0e02b6d6b` -> `10ffa1e407d3` |
| `paraphrase_most_complaints` | CHANGED | CHANGED | `bd8f3df4a05c` -> `f66b4cd9c99c` |
| `paraphrase_vendors_poor_experience` | CHANGED | CHANGED | `fdd0e02b6d6b` -> `21bd00434669` |
| `paraphrase_suppliers_negative_reviews` | CHANGED | CHANGED | `fdd0e02b6d6b` -> `816b72ccea0b` |

**8 of 8 comparable questions changed on both legs.** Note `fdd0e02b6d6b` in the M0
column: four different questions returned the same SQL evidence. That is F-03 in a
single column. All four are now distinct.

Under F-03 no template bound anything but `:limit`, so the eight completed questions
drew from five pre-baked result sets. Every question now binds filters and a sort key.
Bound values actually used on this run:

| question | sort | SQL filters | graph filters |
|---|---|---|---|
| `seller_complaint_warranty_policy` | `avg_review_score_worst` | `max_avg_review_score: 3.0` | — |
| `late_delivery_logistics_region_guidance` | `delivery_delay_days` | `is_late_delivery: true` | — |
| `product_quality_troubleshooting` | `review_score_worst` | `negative_only: true` | — |
| `seller_negative_ticket_paths` | `late_delivery_rate` | — | — |
| `control_highest_revenue_seller` | `total_item_revenue` | — | — |

**Two routes changed, and both are improvements.** `paraphrase_vendors_poor_experience`
and `paraphrase_suppliers_negative_reviews` moved from `seller_performance` to
`review_intelligence`. Both questions are about negative customer experience, which is
what `review_intelligence` is for; the planner prompt now also carries the filter and
sort vocabulary, which appears to sharpen the routing. The audit noted only 1 of 3
document paraphrases hit the expected route; that measurement is M3's to redo properly
on a held-out set, because these three are not held out.

**Two crashes became results.** Both for schema reasons rather than retrieval reasons —
see §3 and §4.

**Record counts fell on several questions, and that is the point.** `seller_negative_ticket_paths`
returns 2 SQL rows instead of 10 because the evidence is now restricted to sellers the
retrieved documents actually name. Ten rows of unfiltered table was never ten rows of
evidence.

**Answer quality did not degrade.** Nine of ten scored grounding 5. One scored
completeness 4. Under M0 the deterministic half of the pass criterion reduced to
`len(answer) >= 1000`, so these scores measure the same weak instrument as before —
they show nothing regressed, not that quality improved. That distinction is M3's job.

---

## 3. The seam-test finding (Task 1, F-02)

**This is the most transferable lesson in the milestone**, and it is a concrete
instance of the audit's central structural finding (F-12): twelve validators reported
PASS while the retrieval layer was inert, because each validated its own stage and
none covered a handoff.

`compact_vector_result()` read `payload["text_preview"]`; ingest writes
`payload["text"]`. The key `text_preview` existed on **0 of 6,098 points**.

Three tests were written in `tests/test_vector_payload_contract.py`. With the fix
reverted:

```
test_live_point_carries_non_empty_text        PASSED   <- payload-level
test_every_sampled_point_carries_text         PASSED   <- payload-level
test_retriever_compaction_delivers_the_text   FAILED
  AssertionError: 25 of 25 compacted results reach the LLM with no document text (F-02)
```

**The two payload-level tests pass on broken code.** They assert that Qdrant holds the
text — which was always true. F-02 was never about storage; it was about the read. Only
the third test, which pushes a real point through the retriever's own
`compact_vector_result` and asserts on what comes out, can fail.

The rule this yields: **assert on what crosses the boundary, not on what sits on either
side of it.** A test that reads the producer's output and a test that reads the
consumer's input can both pass while the two disagree.

The same reading found a second, unreported instance in the same function:
`payload["artifact_id"]` was also read and never written (ingest writes
`stable_document_id`), so every vector result reached the LLM with `artifact_id: None`
and no retrieved document could be cited. Also 0 of 6,098. Fixed in the same two lines.

---

## 4. The sampling caveat (Task 2, F-01)

The requested test — a random sample of 50 `support_tickets` points, all carrying at
least one entity ID — was written and passes. **It is a weak instrument and should not
be quoted as coverage.**

Injecting a regression that stripped the four join keys from 200 of 3,000
support-ticket points:

```
test_sampled_support_ticket_points_carry_entity_ids   FAILED  (2 of 50 sampled)
test_every_order_scoped_point_carries_entity_ids      FAILED  (support_tickets: 200)
```

The sample caught 2 points. The exact miss probability for a 50-point sample against a
regression of that size is **3.08%** — and it degrades fast for smaller regressions:

| regression size | probability a 50-point sample misses it entirely |
|---|---|
| 50 / 3,000 | 42.9% |
| 100 / 3,000 | 18.1% |
| 150 / 3,000 | 7.5% |
| 200 / 3,000 | 3.1% |
| 300 / 3,000 | 0.5% |

So `test_every_order_scoped_point_carries_entity_ids`, which checks **all 6,098
points**, is the test that actually holds the contract. The sampled test is retained
because it was requested and because it is cheap, but the census is the one that would
catch a partial regression.

**Result: 5,985 of 6,098 points carry entity IDs** — and all 5,985 carry all four, not
just one. Baseline: 0 of 8,152. The 113 without are the 40 policies and 73 guides,
which are scoped to a category rather than an order; they carry `policy_id` (40/40) and
`guide_id` (73/73) instead.

**"Coincidence, not contract."** M1 had already made the generator write entity IDs at
the top level of each record, so the collection showed 5,985 seller IDs *before* Task 2
touched anything. But ingest still read only the top level, while the generator's own
schema puts every ID under `linked_entities`. The IDs survived by duplication, not by
agreement. Stripping the top-level copies from a ticket record and re-running the old
`build_payload` returned no IDs at all; the new `resolve_entity_values()` recovers all
four from `linked_entities`. Any corpus that stopped duplicating them upward would have
silently reproduced F-01 in full.

`assert_join_keys_present()` now fails the ingest rather than shipping a corpus that
cannot join. F-01 survived for months because losing every ID produced no error — the
ingest reported "8,152 points upserted" and every validator passed. The count was never
the thing to check.

---

## 5. Evidence-linked filtering (D-1) — corrected measurement

The vector leg runs first, its top hits are mined for `seller_id` / `order_id` /
`product_id` / `customer_id`, and those IDs are bound into the SQL and graph legs
wherever the planner supplied no entity filter of its own. Flag:
`EVIDENCE_LINKED_FILTERING`, default on; `false` restores independent legs.

**A correction to what was reported at the end of Task 3.** On the single question
measured there — the flagship — the linked IDs intersected the planner's
`max_avg_review_score <= 3.0` to produce zero rows, the empty-result fallback fired on
both legs, and linking contributed nothing. It was reported then that "on the one
question measured, D-1 bought nothing." Measured across the full baseline set, that
impression was unrepresentative:

| | count |
|---|---|
| answerable questions | 9 |
| questions where evidence links were bound to at least one leg | **7** |
| questions where the empty-result fallback fired | **1** (the flagship) |

And it is load-bearing. `paraphrase_vendors_poor_experience` and
`paraphrase_suppliers_negative_reviews` receive **identical** routes, identical planner
filters (`negative_only: true`) and an identical sort key. Their SQL evidence differs
only because the `order_ids` harvested from their respective vector hits differ.
Without evidence linking these two questions would return byte-identical SQL evidence —
precisely the F-03 pattern the milestone exists to remove.

**One observation is still not a measurement, in either direction.** Nine questions is
better than one, but these are not held out and several are the planner's own few-shot
examples. M3 must report, on the 60–100 item held-out set: the share of questions where
linking changes the evidence, the share where the fallback fires, and retrieval
precision with the flag on versus off. If linking does not beat independent legs there,
the honest outcome is to default the flag off and say so.

**Evidence linking must never empty a leg.** Found by running the flagship end to end:
the planner's threshold and nine harvested seller IDs were each reasonable alone and
empty in conjunction, taking both legs from 10 records to 0. Linking is an enrichment
the question did not ask for, so a leg that returns nothing *and* had linked IDs is
re-run with only the planner's own filters, and the confidence signal says so. The
planner's own filters may still legitimately return nothing.

---

## 6. The evaluator was silently discarding a third of the evidence

Found in Task 4 while chasing the policy-ID gap, and it is the largest single finding
of the milestone after the three named defects.

`build_evaluation_prompt` called `compact_json(document_evidence, max_chars=9000)`,
which truncates by chopping the tail off the serialised JSON. On the flagship question
the document evidence was **12,597 characters**. The chop removed the entire
`policy_documents` group: **zero `POL-` identifiers reached the evaluator.**

The evaluator then reported the answer's policy IDs as unsupported claims. It was
right, given what it had been shown, and wrong about the system. Grounding scored 2 and
the run FAILed.

Task 3 recorded this as "the IDs are in the prompt — an evaluator-calibration issue for
M3." **That was wrong and is corrected here:** the IDs were in the *answer* prompt and
absent from the *evaluator* prompt. It was a context-assembly defect, not a calibration
one.

`fit_document_evidence()` now shrinks to the budget without losing a group — shortening
previews first, then records per group, never below one record per group — and reports
what it trimmed rather than dropping it silently. Same failure family as F-06.

Measured effect on the flagship question, same answer, same evidence:

| | before | after |
|---|---|---|
| overall status | FAIL | **PASS** |
| grounding | 2 | **5** |
| completeness | 3 | **5** |
| business readiness | 2 | **5** |
| unsupported claims | 3 policy IDs | none |

**Attribution was isolated, not assumed.** Three changes landed together: surfacing
`policy_id` as a structured field, the truncation fix, and a prompt rule stating that an
identifier appearing in the evidence is supported. Re-running with the prompt rule
removed and only the truncation fix in place still gave grounding 5 and PASS, so the
truncation fix alone accounts for the change.

A two-point grounding swing caused by prompt assembly rather than answer quality is
exactly the sensitivity an evaluation number must not have. `tests/test_evaluation_budget.py`
(8 tests) pins it, including a characterisation test of the old tail-chop behaviour.

---

## 7. Limitation: the policy template reaches 24 of 40 policies

`category_policy_guide_paths` begins
`MATCH (p:PolicyDocument)-[:APPLIES_TO_CATEGORY]->(c:Category)`, so a policy without a
category edge cannot be returned by it, whatever the filter says.

```
scope                 policies   APPLIES_TO_CATEGORY edges
all_sellers                  8                           0
category_scoped             24                         264
high_volume_sellers          8                           0
```

The 8 `all_sellers` and 8 `high_volume_sellers` policies are therefore **structurally
unreachable** through that template. This is a consequence of the corpus design
(`docs/CORPUS_DESIGN.md` Part 4): an "all sellers" policy stores a selection *rule*
rather than enumerating 3,030 seller IDs, and by the same logic it is not attached to
each of the 73 categories.

The seller filter added in Task 3 is written to keep `all_sellers` policies — a policy
matches if it has an `APPLIES_TO_SELLER` edge to one of the requested sellers **or** its
scope is `all_sellers` — so the predicate is correct, but that branch is currently
unreachable via this route.

Those policies do still reach the answer: the vector leg carries `policy_seller_ids`,
`policy_categories` and `policy_seller_selection_rule` on every policy point (added in
Task 2), and the policy documents themselves are retrieved semantically.

The first version of the test asserted that `all_sellers` policies came back under a
seller filter. It failed. Rather than widen the template's `MATCH` — a change to the
traversal shape, which is a D-4-class decision — the test was corrected to the measured
truth and the limitation pinned by
`test_policy_template_reaches_only_category_scoped_policies`, which fails if the
coverage ever changes so the note here cannot go stale unnoticed.

---

## 8. A `sys.path` leak that hid an import collision

`tests/test_repo_hygiene.py:277` did a bare `sys.path.insert(0, str(SRC))` and never
undid it. For the remainder of the pytest session `C:\enterprise_ai\src` sat at
`sys.path[0]`, after which `import observability` resolved to the empty package
`src/observability/__init__.py` instead of the module
`src/observability/observability.py`.

The symptom: `tests/test_workflow_routing.py` passed when run alone and failed with
`ImportError: cannot import name 'new_run_id' from 'observability'` in the full suite.

Latent since M1. It surfaced now because Task 3 added the first test that imports
`agentic_workflow`, and that module is the only importer of `observability` by bare
name. Changed to `monkeypatch.syspath_prepend`, which pytest restores after the test.

Worth recording because the failure mode is order-dependent: a suite that happened to
run its tests in a different order would have shown it earlier or never.

---

## 9. Frozen-defect tests that were deliberately changed

M0 recorded the defects as passing tests with docstrings saying "M2 changes this; M0
records it." Six assertions in `tests/test_allowlist_frozen.py` were updated:

| was | now |
|---|---|
| `("sql_intent", None)` rejected | null intents accepted; two further injection strings added in their place |
| `("graph_intent", None)` rejected | as above |
| `vector_artifact_groups=[]` rejected | legal when another leg is selected |
| `vector_artifact_groups=None` rejected | normalised to `[]` |
| `test_sql_only_plan_is_currently_unrepresentable` | `test_sql_only_plan_is_representable`, plus graph-only and vector-only cases |
| `test_templates_bind_only_limit` | `test_templates_bind_exactly_their_declared_parameters` |

**Every injection-shaped case is retained and two more were added.** The allowlist
itself is unchanged: still 6 SQL intents, 5 graph intents, 6 vector groups. Three new
tests close the gap the relaxation opens — an answerable plan selecting no leg is
rejected, an unanswerable one is accepted as a refusal, and every template placeholder
must be a declared parameter of that template.

---

## 10. Other fixes made along the way

- **The Neo4j `Category` nodes were stale.** All 73 had no
  `product_category_name_english`, so three of the five Cypher templates returned
  `category: null` to the LLM. F-04's residue: the loader code was correct but the graph
  was loaded while that PostgreSQL column was NULL, and Neo4j drops null properties on
  `SET n += row`. Re-running the Category load through the loader's own queries fixed
  it: **0 → 73**.
- **Severity ordered alphabetically.** `ORDER BY t.severity DESC` gave
  `medium > low > high > critical`. Measured before: `medium, medium, medium`; after:
  `critical, critical, critical`. Every severity ordering now uses a rank `CASE`.
- **Three Cypher templates had no `ORDER BY` at all**, so their "top ten" was whatever
  the planner emitted. All five now have a deterministic order with a unique tiebreaker,
  as do all six SQL templates — the same determinism requirement M1 imposed on the
  generator, applied to retrieval.
- **Policy documents had no identifier in the Qdrant payload.** The generator writes
  `document_id`; ingest looked up `policy_id`. All 40 now carry one.
- **`sort_by` was miscounted as a filter**, which made a question with no filters at all
  ("Which seller had the highest revenue?") score as "every filter dropped" and get
  flagged low confidence.

---

## 11. What M2 did not do

- **Document truncation is still 9.4%.** `docs/CORPUS_DESIGN.md` D-6 proposed embedding
  the generator's authored `document_text` instead of rebuilding it, which would close
  F-10 at the same time as F-01. It was deliberately not done: changing the embedded
  string changes every vector and therefore every retrieval result, and this milestone's
  baseline diff had to stay attributable to the payload and the parameterisation. F-10
  needs its own change with its own before/after.
- **No new allowlist intents** (D-4). A dedicated policy→seller template would serve the
  flagship question better than reusing `category_policy_guide_paths`, but widening the
  allowlist needs its own adversarial re-test.
- **The evaluation instrument is unchanged.** Every score in §2 comes from the same
  Gemini judge with no measured human agreement. M3 calibrates it.
- **The five validation cases are still the planner's own few-shot examples.** Nothing
  in §2 is a held-out measurement.
