# M5 — cost and latency

Computed from `tests/eval/results/run_20260825T145424Z_full2.json` (82 items, 204 LLM
calls, zero crashes). Analysis of recorded data; no model calls were made to produce it.

Token counts are **measured** — `missing_usage_calls: 0`, so all 204 calls reported
usage metadata. Latency is measured per call by `src/observability/llm_usage.py`.

## Pricing

**`gemini-3.1-flash-lite`: $0.25 per 1M input tokens, $1.50 per 1M output tokens.**

### Correction to previously reported figures

M3 quoted **$0.105** for the 82-item run against an *assumed* rate of $0.10/$0.40,
explicitly flagged at the time as an assumption rather than a measurement. The published
rate is 2.5× higher on input and 3.75× on output.

**The corrected figure is $0.2972 — 2.8× the number reported in `docs/M3_RESULTS.md`
§6.** Every cost figure in this document uses the published rate. The M3 token counts
were correct; only the price applied to them was wrong.

---

## Per agent

| agent | calls | in mean | in p95 | out mean | out p95 | cost | $/call |
|---|---:|---:|---:|---:|---:|---:|---:|
| planner | 82 | 2,928 | 2,951 | 145 | 224 | $0.0779 | **$0.00095** |
| answer | 61 | 2,337 | 3,984 | 677 | 1,059 | $0.0975 | **$0.00160** |
| evaluator | 61 | 6,514 | 9,522 | 245 | 280 | $0.1218 | **$0.00200** |

**The evaluator is the most expensive agent in the system — $0.1218, 41% of total
spend.** It consumes 6,514 input tokens per call on average and 9,522 at p95, because
it is handed the SQL evidence, graph evidence, document evidence, entity IDs and
cross-references in order to judge grounding.

Read alongside M3's calibration result, that is the uncomfortable number in this table:
**the most expensive agent produces the least informative output.** It scored 5 on 60 of
61 answers (kappa 0.390 against a human rater) while costing more than the agent that
writes the answer.

The planner's input is nearly constant (mean 2,928, p95 2,951) — it is dominated by the
fixed prompt: six SQL intents, five graph intents, six vector groups, per-template
filter vocabularies and sort options, and five worked examples.

---

## Per stage — LLM call latency

| stage | n | mean | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| planner | 82 | 1,678 | 1,192 | 3,555 | 10,465 | 14,842 |
| answer | 61 | 3,562 | 2,682 | 9,175 | 15,385 | 18,718 |
| evaluator | 61 | 2,269 | 1,613 | 6,593 | 12,991 | 15,054 |

All values in milliseconds, measuring the provider call only.

The tails are long relative to the medians — the answer agent's p99 is 5.7× its p50.
These are provider-side variations, not retrieval cost.

## End to end, per item

| | n | mean | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|---:|
| all items | 82 | 12,451 | 13,423 | 22,764 | 27,034 | 27,865 |
| answered | 61 | 15,119 | 13,632 | 23,605 | 27,249 | 27,865 |
| refused | 21 | 4,702 | 4,475 | 4,892 | 10,771 | 12,241 |

**These include 4.5 s of deliberate client-side pacing per LLM call**, added to stay
under the Gemini free tier's 15 requests/minute. An answered item makes 3 calls, so
roughly 13.5 s of every answered item is throttling. Net of pacing, an answered item is
approximately **1,619 ms**.

---

## Cost per query

| | mean | p95 | n |
|---|---:|---:|---:|
| **answered query** | **$0.00456** | $0.00606 | 61 |
| **refused query** | **$0.00092** | $0.00094 | 21 |
| all items | $0.00362 | — | 82 |

**A refusal costs 1/5th of an answer** — one planner call instead of planner, answer and
evaluator. The M4 refusal gate converted four items from answered to refused, which is a
cost reduction as well as a correctness one.

Run total: **779,995 input + 68,153 output tokens = $0.2972 for 82 items.**

---

## Task 1: the embedding-model saving in context

The per-request `SentenceTransformer` construction removed in M5 Task 1 cost a measured
**1,146 ms per request** (old median 2,168.5 ms, warm median 1,022.5 ms).

Expressed against answered-query latency, two figures, because one of them is misleading
on its own:

| denominator | saving |
|---|---|
| answered-query mean latency **as measured in the M3 run** (15,119 ms, includes 13.5 s of pacing) | **7.6%** |
| answered-query latency **net of the evaluation pacing** (~1,619 ms) | **70.8%** |

**The second figure is the one that describes production.** The 4.5 s per-call pacing is
an artefact of running 82 questions through a free-tier quota; a real deployment has no
such throttle. Quoting 7.6% would understate the fix by an order of magnitude, and
quoting 70.8% without stating the denominator would overstate it. Both are given.

The saving is pure latency — it changes no token count and therefore no cost.

---

## What this table says about where to optimise

1. **The evaluator is 41% of spend and near-constant in output.** Sampling it — running
   it on a fraction of queries rather than all — would cut roughly a third of total cost
   with no measured loss of information, given kappa 0.390. Not done here; it is a
   change to the measurement instrument and belongs with a decision about whether to
   keep the judge at all.
2. **Refusing early is the cheapest correctness win available.** Each refusal avoids
   two of the three LLM calls.
3. **The planner's prompt is nearly constant at ~2,928 tokens and is paid on every
   query including refusals.** It is the floor on cost per query.
4. **Retrieval is not the latency problem.** Net of pacing, the three database legs plus
   embedding sit at roughly 1.6 s against 7.5 s of provider call time.
