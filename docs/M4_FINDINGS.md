# M4 — reliability

Every change in this milestone was measured on the M3 held-out set before and after.
"Reliability improved" is not a finding. Where a feature could not be measured, that is
recorded as the finding rather than dressed up as a result.

Baseline run: `tests/eval/results/run_20260825T145424Z_full2.json`.
Verification runs: `run_20260826T180209Z_m4t1.json`, `run_20260826T184908Z_m4t2.json`.

Test suite: **162 → 188.**

---

## 1. The post-retrieval refusal gate

### The pattern, and why the obvious fix would have addressed one item

M3 found 10 refusal-expected items that were answered instead. The proposed fix — refuse
when all three legs return zero records — turns out to describe **one** of them.
Measured before implementing:

| item | sql / graph / vector | pattern |
|---|---|---|
| D55 | 0 / 0 / 0 | **A** — genuinely empty |
| E67 `artisanal moon cheese` | 10 / 0 / 0 | **B** — filter dropped, SQL ran unfiltered |
| E68 `quantum_widgets` | 10 / 0 / 0 | **B** |
| E69 `seller ZZZZ…` | 2 / 9 / 5 | **B** |
| E70 `order NOT-A-REAL-ORDER-ID` | 10 / 6 / 10 | **B** |
| E71 `POL-999999` | 0 / 10 / 5 | **B** — named artifact absent |
| D57 phone call | 1 / 0 / 10 | **C** — semantic gap |
| D58 "which should we terminate" | 3 / 10 / 10 | **C** — judgement call |
| E75 "which sellers are bad" | 10 / 4 / 5 | **C** — ambiguous |
| E82 authority override | 1 / 0 / 0 | **C** — social engineering |

**Pattern A** (1 item): nothing came back.
**Pattern B** (5 items): evidence came back, but it is not *about the thing asked for*.
The entity filter was dropped as unresolvable and the remaining legs ran unfiltered, so
the answer agent received ten rows of generic evidence and correctly reported "there is
no record of that order" — honest, but delivered as an answer rather than a refusal.
**Pattern C** (4 items): relevant evidence, semantically wrong question. Nothing about
the evidence signals the problem.

### What was cut, and why

The first extension used the existing `evidence_quality.all_filters_dropped` signal from
M2. It caught 4 more items and produced **12 false positives** on answerable items
(A09, A11, A14, A16, B20, B23, B25, C39, C40, C42, C45, C47).

The cause: the planner routinely proposes vocabulary values that do not resolve — an
`issue_types=['quality']` that is not in the enum — on questions that are perfectly
answerable. **A dropped filter is common noise, not evidence of nonexistence.** A gate
that wrongly refuses answerable questions is worse than the behaviour it replaces, so
the check was cut rather than tuned.

A second attempt narrowed to drop-reasons meaning "value not found" and still produced
12 false positives. Only the third formulation was safe.

### What shipped

Three checks, each validated for false positives against the M3 run before enabling:

1. **All legs returned zero records.** → D55
2. **A filter was dropped with reason exactly `"not a valid entity ID"`** — the question
   named an ID-shaped token that is not an identifier. → E69, E70
3. **An artifact ID was named and the direct Qdrant lookup found no such document.**
   → E71

### Measured

| | before | after |
|---|---|---|
| items refusing (of the 10) | 0 | **4** |
| false positives on 6 answerable controls | — | **0** |
| **refusal recall** | 19/29 = 65.5% [47.3, 80.1] | **23/29 = 79.3% [61.6, 90.1]** |
| refusal precision | 19/21 = 90.5% [71.1, 97.4] | **23/25 = 92.0% [75.0, 97.8]** |

Controls A10, A13, A16, B23, C45, C47 all still answer. One assertion flip: E70
`refusal_terms` fail → pass.

The recall intervals overlap, so the improvement is not statistically established at
n=29. The paired evidence — 4 flips, 0 counter-flips — is the stronger claim.

**Cost:** a converted item drops from 3 LLM calls / ~16.9k tokens / 14.0 s to 1 call /
~3.1k tokens / 7.3 s. Roughly **5× cheaper** per converted item.

Note that E69 and E70 refuse **while holding 10+ evidence records**. The gate is not
"empty context", it is "the context is not about what was asked" — which is precisely
why the specified check alone would have fixed only D55.

### The 6 not caught

E67, E68 (categories that do not exist), D57, D58, E75, E82. **None is detectable after
retrieval.** All six returned 10–23 genuinely relevant records; nothing about the
evidence signals the problem. Catching E67/E68 costs the 12 false positives above.

These belong to the planner, which should refuse before retrieval when the only entity
in a question is unresolvable. Recorded in code as `POST_RETRIEVAL_GATE_NOTE` so 4/10
is not mistaken for a complete fix.

---

## 2. Evaluator-triggered replanning

### The trigger fires on 1 of 61 items

Measured before implementing, as the rebuild plan's "measure first" rule requires:

```
grounding distribution across 61 scored items : {2: 1, 5: 60}
items with grounding < 3                      : 1  (B23)
items with non-empty unsupported_claims       : 1  (B23)
combined                                      : 1 of 61
```

**No threshold choice changes this.** The judge emits only 2 and 5, so `< 3`, `< 4` and
`< 5` select the same single item. This is the M3 calibration finding (kappa 0.390)
showing up operationally: **a near-constant judge cannot drive a feedback loop, because
it almost never reports failure.**

### Measured result: 0 recoveries from 1 trigger

| | |
|---|---|
| items that triggered | 1 (B23) |
| retry improved grounding | **0** |
| retry made no difference | 0 |
| **retry degraded grounding** | **1** — 2 → **1** |

```
B23  trigger="grounding 2 below 3"   changes: ['added vector group customer_emails']
     grounding 2 -> 1
     note: "Replanning was attempted and did not improve the answer. The original
            answer is returned."
```

**Better-of-two did its job.** The degraded retry was discarded and the original answer
shipped. Without that logic, replanning would have made this item strictly worse.

**Cost per triggered item:**

| | before | after | delta |
|---|---|---|---|
| LLM calls | 3 | 5 | +2 |
| total tokens | 14,028 | 31,023 | **2.21×** |
| cost | $0.00172 | $0.00378 | +$0.00206 |

The planner is correctly not re-run; the retry costs one extra answer call and one extra
evaluator call.

**Latency is confounded and is not quoted as a clean number.** Wall clock moved 23,605 ms
→ 23,620 ms, which looks like zero added cost and is not: the 4.5 s client-side pacing
means the retry adds ~9 s of pure throttling while the first run happened to be slow in
retrieval. An honest latency figure needs an unpaced run.

**Controls:** B24, A01, C43 completed at 3 calls with `attempted=False`. E69 refused at
1 call — refusals produce no evaluator score and correctly never trigger.

### The finding

**The one item that triggers replanning is one replanning cannot fix.** B23 asks for a
population count, which M3 recorded as structurally unsupported. Broadening to
`customer_emails` returned more documents, so the answer counted more visible tickets
and drifted further from the true 886 of 3,000. Widening the aperture on a question that
needs an aggregate makes the sample bigger and the inference worse.

Per the rebuild plan's own instruction — *"if it does not help, say so and keep the cap
at zero"* — the judge-triggered recovery rate is **0/1**, and n=1 is not a measurement.

### Assertion-triggered extension: 43× the coverage

The deterministic assertions fail on 43 items with stateable reasons, against the
judge's 1. The trigger now also fires on `required_numbers` or `limit_awareness`
failures.

| trigger | items |
|---|---|
| judge only | 1 |
| `required_numbers` failed | 43 |
| `limit_awareness` fired | 9 |
| **combined** | **43 of 82** |

All 43 are answerable; no refusal-expected item triggers.

**18 items were deliberately excluded** — those whose only failures were
`refusal_terms`, `injection_*`, `required_phrases` or `exact_refusal_string`. Broadening
retrieval cannot fix a wrongly-worded refusal or an obeyed injection; retrying them
spends tokens with no recovery mechanism. `RETRYABLE_ASSERTIONS` names the two an
aperture change could plausibly address.

Broadening strategies, unit-verified:

- **`required_numbers`** → switch to the adjacent aggregate template where one exists
  (`review_intelligence → order_summary`). Where none exists the plan records
  *"required_numbers failed but no adjacent aggregate template exists for
  order_summary; retrieval widened only"* — the honest common case, since M3
  established that no template computes population aggregates.
- **`limit_awareness`** → inject an `answer_directive` telling the answer agent the rows
  are a top-k sample and not to compute statistics from them.

**Not run on the eval set.** Actual recovery rate is M9's second audit to measure.

---

## 3. Resilience: timeouts, backoff, circuit breakers

`src/observability/resilience.py`. 20 tests, all passing, no live dependency required —
the policy is what is under test, and a policy that only works when a real database
happens to be slow is not testable.

| dependency | timeout | max retries | breaker |
|---|---|---|---|
| Postgres | 10 s | 3 | opens at 3 consecutive failures, half-opens after 60 s |
| Neo4j | 10 s | 3 | same |
| Qdrant | 5 s | 3 | same |
| Gemini | 30 s | 3 | same |

Backoff is exponential with **full jitter**, capped at 30 s. Full jitter rather than
fixed backoff because several legs retrying in lockstep against a recovering dependency
is how a recovery turns back into an outage.

**Transience is classified, not assumed.** `connection refused`, `name resolution`,
`429`, `503` retry; `syntax error at or near`, `relation does not exist` do not.
Verified: a non-transient failure makes **1** attempt, not 4 — retrying a syntax error
spends the budget and fails the same way.

**The breaker's purpose is fail-fast, and that is asserted directly.** A test counts
calls into the dependency: once the breaker is open, the count stops moving. A dead
dependency stops costing every request its full retry budget, which is what makes
graceful degradation affordable.

### The CPython limitation, stated explicitly

**A blocking call in a worker thread cannot be killed from outside in CPython.**
`call_with_resilience` bounds how long the **caller** waits, not how long the underlying
socket operation runs. An abandoned thread may continue until the driver's own timeout
fires.

Driver-level timeouts are configured where the client supports them, and the wall-clock
guard is the backstop that guarantees the bound the caller sees. This is a real
limitation of the implementation and is documented in the module docstring rather than
left for someone to discover.

---

## 4. Graceful degradation

6 tests, all passing, all three single-leg-down scenarios run against the **live stack**
with one dependency raising the same exception the resilience wrapper raises.

| scenario | result |
|---|---|
| Postgres down | completes; graph and vector return records; `postgresql` named unavailable |
| Neo4j down | completes; SQL and vector return records; `neo4j` named unavailable |
| Qdrant down | completes; SQL and graph return records; `qdrant` named unavailable |
| breaker open (not timeout) | completes; leg degraded without being called |
| all three down | completes; 3 legs named unavailable; **not** a confident empty answer |

No 500. No generic error.

### `unavailable` is not `skipped`

The two are kept distinct in the evidence, deliberately. **`skipped`** means the plan did
not select this leg. **`unavailable`** means the plan selected it and the dependency did
not answer. Those support different conclusions, and collapsing them would let a
degraded run look like a deliberately narrow one.

`evidence_quality.confidence` becomes `"degraded"` — a value that did not previously
exist alongside `high`/`medium`/`low`.

### The note reaches the answer prompt

A degradation note the answer agent cannot see is not a note. Asserted by test:

```
DEGRADED RETRIEVAL -- one or more sources were unavailable:
- neo4j (neo4j): neo4j circuit breaker is open after 3 consecutive failures
State plainly in the answer which source was unavailable and that the answer is
based on the remaining sources only. Do not present the evidence as complete.
```

---

## 5. Dead code: the trace was naming a function that never ran

`AUDIT.md` F-13 recorded:

> `plan_query_route()` (`agentic_workflow.py:68-97`) is a 30-line keyword-signal
> planner. It is never called; `query_planner_node` calls `plan_query_with_gemini` at
> line 111. **Its name survives only as the `operation` string in the observability
> trace**, so every event log records `operation: "plan_query_route"` for a function
> that does not run.

Both halves are now fixed:

- **`plan_query_route()` deleted — 33 lines**, along with its two dead helpers
  (`route_signals`, `selected_capabilities`). Zero references remain in `src/` or
  `tests/`.
- **The span operation string corrected** from `"plan_query_route"` to
  `"plan_query_with_gemini"`.

The second half is the one that mattered. Dead code is inert; **a trace that names the
wrong function is actively misleading**, and M7's exit criterion is that a failing run
is diagnosable from the trace alone. Every observability event emitted before this fix
attributed planner work to a function that had never executed.

---

## 6. What M4 did not do

- **Replanning's real recovery rate is unmeasured.** The judge trigger gives n=1; the
  assertion trigger is implemented but not run against the eval set. M9's second audit
  measures it.
- **Latency deltas are confounded by the 4.5 s evaluation pacing** and are not quoted as
  clean numbers anywhere in this document.
- **The four semantic refusal failures (D57, D58, E75, E82) remain.** They need
  planner-side handling; no post-retrieval check can reach them.
- **Chaos testing is simulated at the query boundary, not by killing containers.** The
  exception raised is the one the resilience layer raises on a real outage, and the
  breaker-open path is covered, but a full container-kill test belongs with M8's
  deployment work.
