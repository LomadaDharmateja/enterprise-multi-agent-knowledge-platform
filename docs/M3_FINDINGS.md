# M3 — findings from the held-out evaluation

Running the 83-item held-out set surfaced defects that no earlier milestone could have
found, because no earlier milestone had questions the system had not already seen.
This document records them. Metrics and calibration live separately.

---

## 1. Fixed during M3

### 1.1 Evidence-linked filtering falsified population questions (the C43 class)

C43 asks *"for the highest-revenue sellers, does complaint volume scale with order
volume?"*. The planner chose `sort_by=total_item_revenue` correctly. Evidence-linked
filtering (D-1, added in M2) then bound the five `seller_id`s harvested from five
support-ticket hits as a SQL filter, so "highest revenue" was computed over those five.

```
sql filters AS EXECUTED : {"seller_ids": [5 ids harvested from the vector leg]}
rows returned           : 2
top seller revenue      : 1,484.40      (true population top: 229,472.63)
evidence_quality        : {"confidence": "high", "reasons": []}
```

The answer named two sellers with revenues of 1,484 and 629 as "the highest-revenue
sellers" and the judge scored it **grounding 5**.

**Two causes, both fixed.**

**(a) No scope classifier.** A superlative, a total, or a population statistic is a
claim about the whole population; narrowing it to entities that happen to appear in
retrieved documents does not focus the answer, it falsifies it.
`classify_question_scope()` in `hybrid_retriever.py` is a deterministic pattern
classifier -- no model call, so it does not itself become a thing that needs
evaluating -- covering superlatives, totals and population statistics. When it returns
`population`, the SQL leg sets `evidence_link_override: bypass` and evidence linking
does not apply. Measured 11/11 correct on the affected items with no false positives on
entity questions.

**(b) The fallback threshold was too weak.** It re-ran a leg unfiltered only when
`record_count == 0`. C43 returned 2 rows -- non-empty and wrong. The trigger is now
`record_count < EVIDENCE_LINK_MIN_ROWS` (5), and a leg that falls back records
`evidence_link_filtered_count`, `evidence_link_unfiltered_count` and
`confidence_flag: filtered_subset_warning`.

**After the fix**, C43's SQL leg returns 10 rows from the full population, top seller
`4869f7a5dfa277a7dca6462dcf3b52b2` at **229,472.63** over 1,132 orders.

### 1.2 Retrieval could not fetch a document by its identifier

A11 asked for the root cause on ticket `TCK-000002`; the vector leg returned
`TCK-001642`, `TCK-002097`, `TCK-001232` -- semantically similar tickets, none of them
the one named. Same failure on `WRN-000001`, `INC-000001` and `GDE-000002`. Embedding
an identifier and searching for nearby vectors is not a lookup.

`extract_artifact_ids()` recognises the six artifact prefixes and `id_lookup_points()`
issues a Qdrant scroll with `must: [{key, match: {value}}]`. Matched points are
prepended to their artifact group and tagged `retrieval_method: id_lookup`; semantic
results still follow, tagged `semantic`.

One subtlety the verification caught: `ticket_id=TCK-000002` matches **two** points --
the ticket, and `EML-000001`, a customer email whose payload carries that `ticket_id`
because it is a follow-up thread. The email ranked first. Points whose
`stable_document_id` ends with the requested value -- the document that *owns* the
identifier -- now sort above documents that merely reference it.

Result: the correct point is #1 for all five items.

---

## 2. Known, documented, deferred

### 2.1 F-10 — authored document fields never reach the embedded text

**Deferred to the F-10 fix after M3.** Retrieval now returns the right document, and
two items still fail because the field they need is not in it.

`qdrant_ingest.build_document_text()` rebuilds each document from a fixed list of
top-level keys instead of embedding the generator's authored `document_text`. Fields
outside that list are absent from the retrieved text:

| item | field needed | in JSONL | in embedded text |
|---|---|---|---|
| A11 | `root_cause` ("picking error at the seller warehouse") | yes | **no** |
| A15 | `procedure_steps` (the guide's diagnostic steps) | yes | **no** |

Measured consequence, A11 after the retrieval fix:

> "The provided documentation does not explicitly name a 'root cause'."

The correct document was retrieved and the answer is still wrong, which is the
cleanest possible demonstration that these are two separate defects.

This is the same root cause as the truncation issue recorded in
`docs/CORPUS_DESIGN.md` D-6 and deferred in `docs/M2_CHANGES.md` §11: ingest discards
the authored text. Fixing it closes the missing-field defect and the 9.4% truncation
figure together. It changes every embedding, so it needs its own before/after against
this evaluation set rather than being folded into another milestone's diff.

### 2.2 Aggregates are a structural scope limit, not a defect

No SQL template computes `COUNT`, `AVG`, `MEDIAN` or `SUM` over a population; every one
is `SELECT ... ORDER BY ... LIMIT :limit`. Population questions therefore have no route
that can answer them. Affects B18, B19, B20, B23, B24, B26, B28, B29, B30, B31, B32 and
E72.

**Recorded in the README as a scope boundary rather than queued as a fix.** The system
answers questions about specific entities and relationships.

Behaviour on these questions improved during M3 even though the limit remains. Before
the scope-bypass fix, B20 and E72 reported the returned row count as the answer -- `10`
where the truth is 625 and 99,441. After it, they decline:

> "The SQL evidence returned a sample of 10 specific order records, but it does not
> contain a count or an aggregate summary of the total order volume in the database."

A confident wrong number became an accurate statement of what cannot be computed. The
deterministic `required_numbers` assertion still fails these items, correctly -- the
required figure is absent -- but the failure mode is now honest rather than misleading.

---

## 3. Measurement-harness issues, not system defects

### 3.1 The first full run was void

26 of 82 items crashed on `429 RESOURCE_EXHAUSTED`. The Gemini free tier allows 15
requests per minute; an answered item makes three calls, so an unpaced run bursts to
roughly three times the quota. The error carries its own `Please retry in 38.1s` hint,
which the runner was discarding.

Fixed in the harness only: opt-in pacing (`LLM_MIN_INTERVAL_SECONDS`, off by default so
product behaviour is unchanged) plus item-level retry that honours the server's hint.
The re-run completed 82/82 with zero crashes and zero retries needed. This is not the
M4 reliability work; it is the measurement harness not exceeding a quota.

### 3.2 Two labelling errors found by the results

- **C34 and C38** were scored as routing misses solely for omitting `support_tickets`
  from the vector groups. Both answered correctly without ticket documents; requiring
  that group was a labelling error. Corrected, C-category routing is 16/16 and overall
  routing is 50/53 = 94.3% [84.6, 98.1].
- **`required_numbers` counts two unit-format mismatches as failures**: A04's label says
  `0.375` and the answer says `37.5`; B22's says `0.6667` and the answer says `66.7`.
  Same values, different representation. Excluding those, the revised pass rate is
  7/48 = 14.6% [7.2, 27.2] -- so 41 of 43 failures are genuine, and the correction is
  small.

### 3.3 The judge is insensitive to changes that matter

Recorded here because it is the input to calibration, not a conclusion from it. C43
scored **grounding 5** when its answer was built on the wrong two sellers (revenues
1,484 and 629 presented as the highest in the dataset), and **grounding 5** after the
fix when it named the real top sellers and correctly declined to assert a correlation.
Across the 14 re-run items, grounding was 5 before and 5 after, despite several answers
changing substantively.

Whether that is a calibration failure or a rubric that does not measure this property
is what Cohen's kappa against hand labels is for.

---

## 4. Judge calibration

**Linear weighted Cohen's kappa = 0.390, 95% CI [0.000, 0.788], n = 25.**

**This is below the 0.7 threshold the rebuild plan set, and it is reported as measured.**
The LLM evaluator's grounding score does not agree with a human rater well enough to be
used as a quality metric on its own.

### Method

25 answers were hand-scored blind from `tests/eval/calibration/blind_labels.md`: the
file carried the question, the answer and an evidence digest, with the item id, the
category, the eval-set order and the evaluator's score all withheld, and entries
shuffled under a fixed seed. Both raters worked from the same rubric -- the evaluator's
own instruction text -- narrowed to grounding only. The key was opened after the labels
were returned.

Linear weights, so a one-point disagreement is penalised less than a two-point one.
The confidence interval is a percentile bootstrap over 10,000 resamples (seed
20260826); 13 resamples produced an undefined kappa and were dropped.

### The pairs

| # | item | run | human | evaluator | diff |
|---|---|---|---|---|---|
| 1 | A05 | full2 | 4 | 5 | +1 |
| 2 | A01 | full2 | 4 | 5 | +1 |
| 3 | B24 | full2 | 5 | 5 | 0 |
| 4 | A13 | full2 | 5 | 5 | 0 |
| 5 | A11 | full2 | 5 | 5 | 0 |
| 6 | E72 | m3fix | 5 | 5 | 0 |
| 7 | E68 | full2 | 5 | 5 | 0 |
| 8 | B21 | full2 | 5 | 5 | 0 |
| 9 | E69 | full2 | 5 | 5 | 0 |
| 10 | C41 | full2 | 5 | 5 | 0 |
| 11 | B31 | full2 | 5 | 5 | 0 |
| 12 | C48 | full2 | 5 | 5 | 0 |
| 13 | B29 | full2 | 5 | 5 | 0 |
| 14 | A02 | full2 | 5 | 5 | 0 |
| 15 | B20 | m3fix | 5 | 5 | 0 |
| 16 | B28 | full2 | 5 | 5 | 0 |
| 17 | E67 | full2 | 5 | 5 | 0 |
| 18 | B22 | full2 | 5 | 5 | 0 |
| 19 | **B18** | m3fix | **3** | **5** | **+2** |
| 20 | C33 | full2 | 5 | 5 | 0 |
| 21 | B23 | full2 | 2 | 2 | 0 |
| 22 | **E75** | full2 | **3** | **5** | **+2** |
| 23 | **B25** | full2 | **3** | **5** | **+2** |
| 24 | A09 | full2 | 5 | 5 | 0 |
| 25 | E78 | full2 | 5 | 5 | 0 |

### Why kappa is low while raw agreement looks high

Exact agreement is **20/25 = 80%** and within-one-point agreement is **22/25 = 88%**.
Those numbers look healthy and are almost entirely an artefact of the base rate.

```
human score distribution      : {2: 1, 3: 3, 4: 2, 5: 19}
evaluator score distribution  : {2: 1, 5: 24}
```

**The evaluator emitted only two distinct values across 25 answers, and 24 of them were
5.** It is very nearly a constant function. A rater that always says 5 would score 76%
raw agreement against this human sample while carrying no information at all, which is
exactly what kappa is designed to expose and why the raw agreement figure must not be
quoted on its own. Unweighted kappa is lower still at 0.256.

The confidence interval **[0.000, 0.788]** is very wide. At n=25 with one rater's
marginals this degenerate, the estimate is unstable: the data are consistent with no
agreement beyond chance and with agreement above the 0.7 threshold. The point estimate
is the best available answer, and it is not a precise one. A larger hand-labelled
sample would narrow it; nothing here justifies quoting 0.390 as a settled figure.

### Disagreements of 2 or more points

All three run the same way: the human penalised over-generalisation from a top-k
sample, the evaluator did not, and in every case the evaluator recorded **no
unsupported claims**.

**#19 — B18, "Which state has the most sellers registered in it?" — human 3, evaluator 5**

> "the state of **SP** (São Paulo) has the highest concentration of registered sellers
> in the provided sample set. Out of the five specific seller records identified with
> state information, four are registered in SP and one is registered in MG."

*Cause:* the answer generalises a superlative from 10 returned rows to a population
claim. The true figure is 1,849 SP sellers against 349 for PR. The answer does carry a
limitation paragraph saying the sample may not represent the dataset, and the evaluator
appears to have treated that caveat as sufficient grounding, whereas the human treated
the headline claim as unsupported regardless of the caveat below it.

**#22 — E75, "Which sellers are bad?" — human 3, evaluator 5**

> "the following sellers demonstrate significant performance issues... Seller
> `4342d4b2...` Lowest average review score (1.26)."

*Cause:* "lowest" is a population superlative asserted from a 10-row slice, and the
question itself is ambiguous — "bad" is undefined. The answer does state its definition
in the limitations, which the evaluator credited. The human scored the unsupported
"lowest" claim down.

**#23 — B25, "Compare average review scores between sellers in São Paulo and Rio de
Janeiro" — human 3, evaluator 5**

> "the data is insufficient to provide a comparison. While there are records for sellers
> based in São Paulo, there are no records available for sellers located in Rio de
> Janeiro."

*Cause:* the retrieval genuinely returned no RJ sellers, so the answer's account of its
own evidence is accurate — which is what the evaluator rewarded. But it then reports
two individual SP sellers' scores (1.80, 3.40) as though they characterise the state,
when the population figures are SP 3.978 and RJ 3.977, effectively identical. The human
scored the misleading framing down; the evaluator saw a correctly-hedged answer.

### What this means for the other numbers

Every grounding figure reported elsewhere in M3 comes from this instrument. On this
sample it distinguishes almost nothing: **grounding 5 was awarded to 24 of 25 answers**,
including C43 both when its answer was built on the wrong two sellers (revenues of
1,484 and 629 presented as the dataset's highest) and after the fix when it named the
real top seller at 229,472. The mean grounding of 4.95 across the full run should be
read as "the judge almost always says 5", not as a quality measurement.

The deterministic assertions do not have this problem — they disagree with the system
constantly and for stateable reasons — which is the argument for weighting them over
the judge when M3's headline metrics are assembled.

**Not corrected here.** Fixing the judge (a stricter rubric, forced score dispersion, or
a different model) would change the instrument mid-measurement. The honest record is
that the judge was calibrated, found wanting at kappa 0.390, and that every judge-derived
number in this milestone carries that caveat.
