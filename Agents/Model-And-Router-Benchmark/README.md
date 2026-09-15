# Azure OpenAI Model and Model Router Benchmark

[![CI](https://github.com/david-xinyuwei/david-share/actions/workflows/model-and-router-benchmark-ci.yml/badge.svg)](https://github.com/david-xinyuwei/david-share/actions/workflows/model-and-router-benchmark-ci.yml)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab)](requirements.txt)
[![Direct matrix](https://img.shields.io/badge/direct_matrix-11_arms-b11f4b)](scenario-model-benchmark/README.md)
[![Region](https://img.shields.io/badge/client%2Fresource-same_region-16a34a)](#methodology)
[![License](https://img.shields.io/badge/license-MIT-5c5c5c)](../../LICENSE)

Choosing a model for a latency-sensitive assistant is not a leaderboard lookup: the
answer depends on the prompts, the reasoning effort, the API path and the deployment
capacity. This project measures three candidate models across every supported
`reasoning_effort`, compares three Model Router modes against their own direct
baselines, and then tests the two things a pilot usually discovers in production —
multi-turn session cost and behaviour at the rate limit. All 4 studies ran from a
Linux VM in the same Azure region as the deployments, with no web search and no
tools, so what is compared is native model capability. Across 561 measured requests
the median first token arrived between **0.367 s and 15.47 s** and cost ranged from
0.094 to 4.564 USD per 1,000 requests — a 42× latency and 49× cost spread driven by
configuration rather than by model name.

> Author: **Xinyu Wei (魏新宇)**

[English](README.md) | [中文](README-CN.md)

[Executive summary](#executive-summary) · [Methodology](#methodology) · [Results](#results) · [Reproduce](#reproducing) · [Model Router guide](https://learn.microsoft.com/azure/ai-foundry/openai/how-to/model-router)

---

<a id="executive-summary"></a>
## Executive Summary

**No single model wins. Pick per surface against an absolute quality bar, a TTFT
budget and real request volume — "higher reasoning effort" did not buy better
answers on these tasks.**

| Candidate | TTFT P50 | E2E P50 | Tokens / turn | USD / 1k requests | Blind judge / 5 | Test conditions |
|---|---:|---:|---:|---:|---:|---|
| GPT-4o mini | **0.367 s** | 2.26 s | 243 | **0.094** | 4.53 | Responses API, `stream=True`, no reasoning parameter, no tools |
| GPT-5 mini `minimal` | 0.567 s | 2.20 s | 402 | 0.603 | 4.55 | Same, `reasoning_effort=minimal` |
| GPT-5 mini `low` | 1.969 s | 3.97 s | 564 | 0.928 | 4.64 | Same, `reasoning_effort=low` |
| GPT-5 mini `medium` | 5.302 s | 7.38 s | 967 | 1.733 | 4.76 | Same, `reasoning_effort=medium` |
| GPT-5 mini `high` | 15.472 s | 17.55 s | 2 383 | 4.564 | 4.79 | Same, `reasoning_effort=high` |
| GPT-5.6 Luna `none` | 1.074 s | **1.95 s** | 366 | 0.324 | 4.92 | Same, `reasoning_effort=none` |
| GPT-5.6 Luna `low` | 1.171 s | 2.42 s | 387 | 0.350 | 4.82 | Same, `reasoning_effort=low` |
| GPT-5.6 Luna `medium` | 1.408 s | 3.14 s | 413 | 0.380 | 4.92 | Same, `reasoning_effort=medium` |
| GPT-5.6 Luna `high` | 1.918 s | 3.08 s | 468 | 0.447 | **4.95** | Same, `reasoning_effort=high` |
| GPT-5.6 Luna `xhigh` | 2.590 s | 3.67 s | 571 | 0.570 | 4.94 | Same, `reasoning_effort=xhigh` |
| GPT-5.6 Luna `max` | 3.414 s | 4.37 s | 697 | 0.722 | 4.88 | Same, `reasoning_effort=max` |

> 51 measured requests per arm (17 prompts × 3 iterations, 1 warm-up discarded), 561
> measured requests over 11 arms. TTFT and E2E are client-observed medians; tokens and
> cost are per-request means at published list prices. Quality is a blind LLM judge over
> 5 dimensions (187 evaluations). Every measured arm is listed above; reasoning-token
> and per-scenario detail is in [3.1 Direct model matrix](#results).

**Configuration this evidence supports**

| Setting | Value | Why |
|---|---|---|
| API path | Responses API with streaming | Chat Completions delivered 497/564 answers as a single burst, which makes TTFT and per-token pace unmeasurable |
| Reasoning effort | Lowest supported value per model | `high` cost 7.6× more and 27× the TTFT on GPT-5 mini for no quality gain in this sample |
| Router mode | `cost` or `balanced`, not `quality` | `quality` routed 48.9% of requests to GPT-5.6 Sol and changed model inside 18 of 18 conversations |
| Rate-limit handling | Stream-aware fallback, not status-code retry | Every rejection arrived inside an HTTP 200 stream; a status-code-only retry never fired |
| Client placement | Same region as the deployment | Cross-region calls add network time that is then misread as model latency |

<a id="background"></a>
## 1. Background

The workload is a cross-device consumer assistant with six user-facing task families:
Next Move, Write For Me, Catch Me Up, Pay Attention, Live Interaction and Creator Zone.
They are latency-sensitive and mostly non-reasoning text tasks, which is why the study
optimises for time-to-first-token and cost per turn rather than for benchmark scores.

| Input | Value | Source |
|---|---|---|
| Candidate models | GPT-4o mini, GPT-5 mini, GPT-5.6 Luna | [`config/models.json`](scenario-model-benchmark/config/models.json) |
| Router deployment | `model-router`, modes `cost` / `balanced` / `quality` | [`model-router-validation`](model-router-validation/README.md) |
| List prices | Per 1M input / cached / output tokens | [`config/pricing.json`](scenario-model-benchmark/config/pricing.json) |
| Model lifecycle | Retirement dates queried from the Models API | [`model_lifecycle_swedencentral.json`](production-readiness/outputs/model_lifecycle_swedencentral.json) |

Lifecycle matters as much as latency: on the day of the query, GPT-5 mini `2025-08-07`
was scheduled to retire on 2027-02-09 and GPT-4o mini `2024-07-18` was already
deprecated for new customers, while the GPT-5.6 family ran to 2028-01-11.

<a id="methodology"></a>
## 2. Methodology

| Control | Implementation | What it does **not** prove |
|---|---|---|
| Native capability only | No web search and no tools on any compared arm | Nothing about search or tool quality |
| Same region | Benchmark VM and Azure AI resource in one region; VM location re-read from IMDS at export time | Global and DataZone serving do not expose the physical GPU region |
| Identical prompts | The same 17 synthetic prompts in every matrix cell | Customer production traffic or multilingual quality |
| Streaming timing | TTFT at the first non-empty text delta, E2E at stream completion | Server compute, prefill or GPU decode time |
| Failure accounting | `max_retries=0`; a stream without usage fails closed | Availability outside the measured windows |
| Cost | Measured usage × published list price, by the model actually served | An Azure invoice; router fees, VM and judge are excluded |

Sample sizes, SDK versions and the per-study matrices are in each study README. The
harness is one file per study and is the same code the live console calls, so a replayed
chart and a freshly measured chart mean the same thing.

**Why the API path is part of the method.** On Chat Completions, 497 of 564 measured
answers arrived with their first and last text chunk less than 50 ms apart. A TTFT
measured that way is delivery, not generation. Re-running the identical prompts on the
Responses API reduced that to 32 of 564 and made per-token pace measurable. Every
latency conclusion in this repository therefore states its API path.

### 2.1 Test set

Two prompt sets, both committed in this repository and both synthetic: written by the
author for the six task families, not sampled from customer traffic.

| Set | File | Prompts | Composition | Used by |
|---|---|---:|---|---|
| Assistant scenarios | [`assistant_scenarios.jsonl`](scenario-model-benchmark/datasets/assistant_scenarios.jsonl) | 17 | Next Move 3, Write For Me 4, Catch Me Up 3, Pay Attention 3, Live Interaction 2, Creator Zone 2; each prompt carries its own answer budget of 80 to 900 tokens | Direct matrix: 11 arms × 17 prompts × 3 iterations = 561 measured requests |
| Router Task B | [`router_taskb.jsonl`](model-router-validation/datasets/router_taskb.jsonl) | 47 | The 17 assistant prompts plus 30 controlled prompts, 10 per difficulty tier, 40 task categories in total | Router study: 10 arms × 47 prompts × 3 iterations = 1,410 measured requests |

Every prompt is scored once per arm by a blind LLM judge: 11 × 17 = 187 evaluations in
the direct matrix and 10 × 47 = 470 in the router study. Difficulty tiers were assigned by
the author before the run; they are labels, not a routing threshold. The public copy
withholds the text of 2 assistant prompts (PA01, PA03) because it named the customer team;
their measurements are retained unchanged and the withheld set is enforced by the
repository gate.

<a id="results"></a>
## 3. Results

### 3.1 Direct model matrix

Every reasoning effort both reasoning candidates accept, plus the non-reasoning
baseline: 11 arms × 6 scenarios, 66 filled cells, 51 measured requests per arm, 561 in
total, 0 API errors and 0 truncated answers.

| Arm | TTFT P50 | E2E P50 | Tokens / turn | Reasoning tokens | USD / 1k requests | Blind judge / 5 |
|---|---:|---:|---:|---:|---:|---:|
| GPT-4o mini | 0.367 s | 2.26 s | 243 | 0 | 0.094 | 4.53 |
| GPT-5 mini `minimal` | 0.567 s | 2.20 s | 402 | 0 | 0.603 | 4.55 |
| GPT-5 mini `low` | 1.969 s | 3.97 s | 564 | 133 | 0.928 | 4.64 |
| GPT-5 mini `medium` | 5.302 s | 7.38 s | 967 | 550 | 1.733 | 4.76 |
| GPT-5 mini `high` | 15.472 s | 17.55 s | 2 383 | 1 950 | 4.564 | 4.79 |
| GPT-5.6 Luna `none` | 1.074 s | 1.95 s | 366 | 0 | 0.324 | 4.92 |
| GPT-5.6 Luna `low` | 1.171 s | 2.42 s | 387 | 15 | 0.350 | 4.82 |
| GPT-5.6 Luna `medium` | 1.408 s | 3.14 s | 413 | 36 | 0.380 | 4.92 |
| GPT-5.6 Luna `high` | 1.918 s | 3.08 s | 468 | 92 | 0.447 | 4.95 |
| GPT-5.6 Luna `xhigh` | 2.590 s | 3.67 s | 571 | 187 | 0.570 | 4.94 |
| GPT-5.6 Luna `max` | 3.414 s | 4.37 s | 697 | 329 | 0.722 | 4.88 |

Grouped by requested model, then by effort from minimum to maximum. On Luna the 6
efforts span 4.82 to 4.95 and do not order by effort.
On GPT-5 mini the judge does rise with effort, 4.55 to 4.79 over 4 steps — a 0.24-point
gain bought with 7.6× the cost and 27× the TTFT. Per-scenario breakdown and blind-judge
detail are in [the scenario study](scenario-model-benchmark/README.md).

### 3.2 Model Router selection

**What was built.** 3 Model Router deployments on the same resource, one per routing
mode — `cost`, `balanced` and `quality` — each `model-router 2025-11-18`, GlobalStandard,
capacity 300, and each with exactly 2 models behind it: `gpt-5.6-sol 2026-07-09` and
`gpt-5.6-luna 2026-07-09` ([deployment record](model-router-validation/outputs/deployment_verification_router.json)).
Every request was sent to the router, never to a model directly; the model that actually
answered was read from `model_selection_details.model_router_details` in the response and
recorded per request.

**What was sent.** The 47 prompts of [2.1 Test set](#methodology), each 3 measured times
plus 1 warm-up per router, once with no `reasoning_effort` and once with `low`. With the 4
direct Sol and Luna baselines that makes 10 arms, 1,410 measured requests and 470
blind-judged answers. All arms used Chat Completions, the only API surface the router
exposes.

**Selection was repeatable on this sample.** All 282 router cells (6 router arms × 47
prompts) returned the same model on all 3 repetitions; 0 switched. The `low` arms routed
every prompt exactly as the no-effort arms did.

**Latency, cost and quality per arm.** Client-observed medians over 141 requests per arm;
cost at list price of the model that actually served each request; quality is the blind
judge over 47 answers per arm ([arm summary CSV](model-router-validation/outputs/router_arm_summary.csv)).
Read the *Bursts* column first: on the direct DataZone baselines most answers arrived in
one burst, so their TTFT is delivery time rather than first-token time, and a router-minus-
direct difference in this table is **not** router overhead — that question is answered by
the paired test below.

| Arm | Served Sol / Luna | TTFT P50 / P90 | E2E P50 | Bursts <50 ms | Router decision P50 ms | Out tok | USD / 1k | Judge / 5 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| `router-sol-luna-cost` | 0 / 141 | 1.50 / 3.88 s | 2.19 s | 29 / 141 | 20 | 399 | 0.50 | 4.91 |
| `router-sol-luna-cost@low` | 0 / 141 | 1.21 / 2.41 s | 1.86 s | 33 / 141 | 20 | 327 | 0.41 | 4.91 |
| `router-sol-luna-balanced` | 6 / 135 | 1.43 / 4.05 s | 2.02 s | 29 / 141 | 20 | 397 | 1.95 | 4.91 |
| `router-sol-luna-balanced@low` | 6 / 135 | 1.23 / 2.98 s | 1.78 s | 34 / 141 | 20 | 325 | 1.28 | 4.91 |
| `router-sol-luna-quality` | 69 / 72 | 1.92 / 4.30 s | 2.59 s | 36 / 141 | 20 | 375 | 7.53 | 4.91 |
| `router-sol-luna-quality@low` | 69 / 72 | 1.79 / 2.99 s | 2.44 s | 34 / 141 | 20 | 309 | 5.93 | 4.96 |
| `gpt-5.6-luna-dz` | 0 / 141 | 1.57 / 5.29 s | 1.57 s | 126 / 141 | — | 372 | 0.46 | 4.86 |
| `gpt-5.6-luna-dz@low` | 0 / 141 | 1.27 / 4.32 s | 1.28 s | 119 / 141 | — | 343 | 0.43 | 4.93 |
| `gpt-5.6-sol-dz` | 141 / 0 | 2.08 / 7.68 s | 2.09 s | 127 / 141 | — | 360 | 11.24 | 4.88 |
| `gpt-5.6-sol-dz@low` | 141 / 0 | 1.78 / 6.82 s | 1.80 s | 125 / 141 | — | 312 | 9.78 | 4.90 |

**Which model answered each prompt, and what it cost.** Each mode cell is the served model,
the TTFT median of the 3 measured requests, and USD per 1,000 requests for that prompt
([per-question CSV](model-router-validation/outputs/router_question_hits.csv),
[per-request CSV](model-router-validation/outputs/router_single_turn_sessions.csv); 2 prompt
texts are withheld in the public copy):

| Tier | Prompt | Category | `cost` | `balanced` | `quality` |
|---|---|---|---|---|---|
| simple | CZ02 | `edit_intent_parsing` | Luna · 0.81 s · 0.12 | Luna · 1.01 s · 0.13 | Luna · 1.27 s · 0.12 |
| simple | CMU03 | `executive_condense` | Luna · 1.00 s · 0.10 | Luna · 1.14 s · 0.09 | Luna · 1.20 s · 0.09 |
| simple | S01 | `factual_lookup` | Luna · 0.69 s · 0.03 | Luna · 1.23 s · 0.03 | Sol · 1.63 s · 0.75 |
| simple | S02 | `factual_lookup` | Luna · 1.50 s · 0.09 | Luna · 1.02 s · 0.09 | Sol · 2.25 s · 2.27 |
| simple | S09 | `faq` | Luna · 1.79 s · 0.30 | Luna · 1.64 s · 0.31 | Sol · 3.29 s · 7.60 |
| simple | S10 | `faq` | Luna · 1.21 s · 0.06 | Luna · 0.79 s · 0.06 | Sol · 1.71 s · 1.52 |
| simple | S07 | `formatting` | Luna · 1.05 s · 0.03 | Luna · 0.83 s · 0.03 | Luna · 0.97 s · 0.03 |
| simple | S08 | `formatting` | Luna · 1.00 s · 0.05 | Luna · 0.76 s · 0.04 | Sol · 1.79 s · 1.21 |
| simple | LI02 | `grounded_followup` | Luna · 1.04 s · 0.07 | Luna · 1.22 s · 0.06 | Sol · 1.68 s · 1.39 |
| simple | PA03 | `instant_recall` | Luna · 1.21 s · 0.05 | Luna · 1.66 s · 0.05 | Sol · 0.84 s · 1.28 |
| simple | S03 | `intent_classification` | Luna · 1.36 s · 0.10 | Luna · 1.36 s · 0.10 | Luna · 1.74 s · 0.09 |
| simple | S04 | `intent_classification` | Luna · 1.79 s · 0.16 | Luna · 1.70 s · 0.15 | Luna · 1.58 s · 0.16 |
| simple | S05 | `short_form` | Luna · 0.77 s · 0.05 | Luna · 1.01 s · 0.04 | Sol · 1.90 s · 1.13 |
| simple | S06 | `short_form` | Luna · 0.69 s · 0.02 | Luna · 0.70 s · 0.02 | Luna · 0.91 s · 0.02 |
| simple | NM03 | `short_suggestion` | Luna · 1.18 s · 0.05 | Luna · 1.26 s · 0.05 | Sol · 2.46 s · 2.09 |
| moderate | M07 | `classification_reasoning` | Luna · 0.93 s · 0.07 | Luna · 0.98 s · 0.08 | Luna · 1.00 s · 0.07 |
| moderate | M08 | `code_snippet` | Luna · 2.07 s · 0.35 | Luna · 1.92 s · 0.31 | Sol · 3.45 s · 10.34 |
| moderate | M04 | `comparison` | Luna · 1.65 s · 0.20 | Luna · 1.20 s · 0.20 | Sol · 1.99 s · 4.66 |
| moderate | LI01 | `conversational_turn` | Luna · 1.04 s · 0.07 | Luna · 0.91 s · 0.07 | Luna · 1.22 s · 0.07 |
| moderate | NM02 | `cross_device_continuity` | Luna · 1.53 s · 0.19 | Luna · 1.52 s · 0.18 | Sol · 2.20 s · 4.32 |
| moderate | CMU02 | `decision_extraction` | Luna · 1.95 s · 0.28 | Luna · 2.03 s · 0.24 | Luna · 2.20 s · 0.29 |
| moderate | M02 | `drafting` | Luna · 0.80 s · 0.11 | Luna · 0.97 s · 0.11 | Sol · 1.94 s · 2.51 |
| moderate | M09 | `planning` | Luna · 1.32 s · 0.26 | Luna · 1.03 s · 0.23 | Luna · 1.42 s · 0.25 |
| moderate | CZ01 | `prompt_expansion` | Luna · 1.08 s · 0.26 | Luna · 1.33 s · 0.30 | Sol · 1.98 s · 6.31 |
| moderate | M06 | `rewriting` | Luna · 0.86 s · 0.03 | Luna · 0.76 s · 0.03 | Luna · 0.90 s · 0.03 |
| moderate | WFM04 | `short_draft` | Luna · 1.86 s · 0.37 | Luna · 1.37 s · 0.34 | Sol · 1.78 s · 6.90 |
| moderate | M03 | `structured_extraction` | Luna · 0.76 s · 0.06 | Luna · 0.88 s · 0.06 | Luna · 1.19 s · 0.06 |
| moderate | M01 | `summarization` | Luna · 1.00 s · 0.07 | Luna · 0.98 s · 0.07 | Luna · 1.28 s · 0.07 |
| moderate | M10 | `summarization` | Luna · 0.82 s · 0.06 | Luna · 0.86 s · 0.06 | Luna · 0.86 s · 0.06 |
| moderate | WFM02 | `tone_continuation` | Luna · 1.54 s · 0.28 | Luna · 1.66 s · 0.24 | Luna · 1.61 s · 0.27 |
| moderate | WFM03 | `tone_shift_rewrite` | Luna · 2.00 s · 0.20 | Luna · 2.53 s · 0.21 | Luna · 2.07 s · 0.16 |
| moderate | PA02 | `translate_and_summarize` | Luna · 1.79 s · 0.29 | Luna · 1.91 s · 0.30 | Luna · 1.94 s · 0.29 |
| moderate | M05 | `troubleshooting` | Luna · 2.38 s · 0.36 | Luna · 2.12 s · 0.31 | Sol · 4.15 s · 7.63 |
| complex | C09 | `ambiguity_resolution` | Luna · 1.45 s · 0.53 | Luna · 1.96 s · 0.48 | Sol · 3.05 s · 11.57 |
| complex | C03 | `architecture_reasoning` | Luna · 2.44 s · 0.89 | Luna · 2.87 s · 0.94 | Luna · 3.02 s · 0.86 |
| complex | CMU01 | `backlog_digest` | Luna · 2.49 s · 0.45 | Luna · 2.41 s · 0.44 | Luna · 2.32 s · 0.43 |
| complex | C06 | `code_reasoning` | Luna · 5.80 s · 1.05 | Sol · 9.45 s · 26.65 | Sol · 8.86 s · 25.32 |
| complex | C04 | `constrained_reasoning` | Luna · 9.50 s · 1.77 | Sol · 14.03 s · 44.86 | Sol · 15.49 s · 48.18 |
| complex | PA01 | `keypoint_capture` | Luna · 1.60 s · 0.56 | Luna · 1.11 s · 0.59 | Luna · 1.13 s · 0.51 |
| complex | WFM01 | `long_form_draft` | Luna · 2.16 s · 3.29 | Luna · 1.72 s · 3.08 | Sol · 3.04 s · 64.92 |
| complex | C08 | `long_form_synthesis` | Luna · 2.00 s · 2.39 | Luna · 1.71 s · 2.37 | Luna · 2.04 s · 2.47 |
| complex | C10 | `multi_constraint_planning` | Luna · 8.29 s · 3.89 | Luna · 7.75 s · 4.31 | Sol · 11.27 s · 92.73 |
| complex | C01 | `multi_step_math` | Luna · 7.92 s · 1.48 | Luna · 8.20 s · 1.28 | Sol · 12.36 s · 30.20 |
| complex | C02 | `multi_step_math` | Luna · 2.48 s · 0.43 | Luna · 2.63 s · 0.52 | Sol · 3.50 s · 11.05 |
| complex | NM01 | `proactive_suggestion` | Luna · 1.69 s · 0.25 | Luna · 1.55 s · 0.23 | Luna · 2.04 s · 0.23 |
| complex | C05 | `root_cause_analysis` | Luna · 3.00 s · 0.59 | Luna · 2.63 s · 0.48 | Luna · 3.55 s · 0.51 |
| complex | C07 | `tradeoff_analysis` | Luna · 2.60 s · 0.95 | Luna · 4.05 s · 1.04 | Luna · 2.96 s · 0.99 |

**Counts per tier.** Read across a row: Sol + Luna equals that row's requests. The
percentage is Sol's share of that tier only, so the column is not meant to sum to 100%.

| Author-assigned tier | Prompts | Requests | `cost`: Sol / Luna | `balanced`: Sol / Luna | `quality`: Sol / Luna |
|---|---:|---:|---:|---:|---:|
| simple | 15 | 45 | 0 / 45 (0.0%) | 0 / 45 (0.0%) | 27 / 18 (60.0%) |
| moderate | 18 | 54 | 0 / 54 (0.0%) | 0 / 54 (0.0%) | 21 / 33 (38.9%) |
| complex | 14 | 42 | 0 / 42 (0.0%) | 6 / 36 (14.3%) | 21 / 21 (50.0%) |
| all tiers | 47 | 141 | 0 / 141 (0.0%) | 6 / 135 (4.3%) | 69 / 72 (48.9%) |

`cost` never chose Sol. `balanced` chose Sol for 2 prompts only, both complex:
`code_reasoning` and `constrained_reasoning`. `quality` chose Sol for 23 of 47 prompts
and its choice did not follow the author-assigned tier — a larger share of simple prompts
went to Sol than of complex ones. What did track the choice was the kind of work requested:
Sol answered prompts that generate content or answer from model knowledge
(`factual_lookup`, `faq`, `code_snippet`, `drafting`, `multi_step_math`, `code_reasoning`)
and Luna answered prompts that transform text already supplied (`summarization`,
`structured_extraction`, `rewriting`, `tone_shift_rewrite`, `keypoint_capture`).

**Does the router add latency? A paired test under identical conditions.** The table
above cannot answer this: the router arms ran on GlobalStandard, the direct baselines on
DataZoneStandard, the arms ran serially, and the direct answers arrived in bursts. A
follow-up run removed all of that. `router-sol-luna-cost` — which routes every prompt to
Luna — was measured against the direct `gpt-5.6-luna` deployment: both GlobalStandard,
both Chat Completions, no `reasoning_effort` on either, the same 45 public prompts, and
each prompt measured as router/direct **pairs back to back**, 1 warm-up pair and 3 measured
pairs, alternating which side went first so ordering bias cancels
([design and executed source](model-router-validation/scripts/paired_overhead.py),
[deployment record](model-router-validation/outputs/paired_deployment_record_20260915.json),
[pairs CSV](model-router-validation/outputs/router_overhead_paired_20260915_162607.csv),
[summary CSV](model-router-validation/outputs/router_overhead_paired_summary.csv)).

| Arm | SKU | n | TTFT P50 / P90 | E2E P50 | Bursts <50 ms | Out tok | USD / 1k | Self-reported router ms P50 / P95 |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| `router-sol-luna-cost` | GlobalStandard | 135 | 2.12 / 4.80 s | 2.65 s | 17.8% | 392 | 0.49 | 20 / 26 |
| `gpt-5.6-luna` | GlobalStandard | 135 | 2.28 / 6.47 s | 2.29 s | 88.9% | 379 | 0.47 | — |

Per-pair difference, router minus direct, on the 135 pairs where both sides were served
by Luna (0 pairs excluded for a different served model). End-to-end time is the
comparable column; the last column is kept to show why TTFT is not:

| Pairs | n | ΔE2E P25 ms | P50 | P75 | P90 | mean | Router slower (E2E) | ΔTTFT P50 ms |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| all | 135 | -97 | 324 | 992 | 1569 | 501 | 71.9% | -147 |
| router sent first | 45 | -97 | 261 | 956 | 2044 | 654 | 71.1% | -158 |
| direct sent first | 90 | -32 | 324 | 992 | 1533 | 424 | 72.2% | -147 |
| simple | 42 | 92 | 488 | 946 | 1350 | 556 | 81.0% | 357 |
| moderate | 54 | -32 | 187 | 775 | 1533 | 390 | 70.4% | -152 |
| complex | 39 | -652 | 264 | 1490 | 5086 | 594 | 64.1% | -1966 |

Reading: the two sides did not stream alike. 88.9% of direct answers arrived in one burst
against 17.8% through the router, so the direct TTFT is mostly whole-answer delivery time
and the TTFT column compares the streaming layer, not the router — it even makes the
router look faster to first token on long answers. On **end-to-end time**, with everything
else held equal, putting the router in front of Luna added **324 ms at the median**
(-97 to 992 ms interquartile, 1569 ms at P90) and the router was the slower side in
71.9% of pairs, whichever side was sent first (261 ms versus 324 ms at the
median), which is what rules out ordering bias. The router's self-reported decision time
was 20 ms at the median, so the added time is mostly the extra hop and its delivery path
rather than the decision itself. Answers through the router averaged 392 output tokens
against 379 direct, from the same model at the same list price; the router fee, if any, is
not in these numbers.

These are observed counts from repeated synthetic prompts, and the task-type reading is
this author's grouping of the 40 categories rather than a published rule. They are not a
production routing probability and not a reconstruction of the internal routing rule.
The paired overhead is one deployment pair, one region, one evening, at concurrency 1.

### 3.3 Sessions, sustained load and the rate limit

- A 4-turn conversation cost 0.90–1.14× the cost of 4 first turns. Prompt caching
  contributed nothing: no request re-sent a prefix long enough to qualify.
- `quality` mode changed the served model inside 18 of 18 conversations; `balanced`
  inside 0 of 18. Tone and per-turn cost therefore move within one conversation.
- Under continuously refilled load the three direct candidates completed every request
  at 4, 8 and 16 in flight. The router deployment at capacity 300 returned 237 in-stream
  rate-limit failures at 16 in flight. **Capacity, not the model, set the ceiling.**
- Every rate-limit rejection observed in the fallback experiment arrived **after an
  HTTP 200, inside the stream**. A client that only retried on request-time status
  never fired and matched the no-fallback result (2.7% success). A stream-aware
  reactive client reached 100%, adding 411 ms P50 to rescued requests.

Details, per-level tables and the fallback client are in
[the production-readiness study](production-readiness/README.md).

### 3.4 Required scope is complete

| Required direct model | Efforts measured | Coverage |
|---|---|---|
| GPT-4o mini | reasoning parameter not sent | direct matrix |
| GPT-5 mini | `minimal`, `low`, `medium`, `high` | direct matrix |
| GPT-5.6 Luna | `none`, `low`, `medium`, `high`, `xhigh`, `max` | direct matrix |

These are exactly the 3 requested direct-model candidates. Their 11 arms cover all
supported effort settings across 6 scenarios: 66/66 cells, 51 measured requests per arm
and 561 measured requests in total. The direct-model test requirement is complete.

GPT-5.6 Sol is **not** the 4th requested direct-model candidate. It appears only in the
Router study as the Router's high-capability option and as a matching direct baseline;
the observations with effort not sent and `low` answer the Router-selection question,
not a Sol effort-sweep question.

The model registry inherited unused candidate entries from the earlier migration
benchmark. Registry presence does not expand this study's scope and does not mean a model
was measured. The
[deployment verification](scenario-model-benchmark/outputs/deployment_verification.json)
is the authoritative record of the 3 required direct deployments.

<a id="cost-analysis"></a>
## 4. Cost Analysis

Cost is computed per request from measured usage and the published list price of the
model that actually served the request, then reported per 1,000 requests. It excludes
DataZone premiums, any router fee, the judge, the benchmark VM and probe traffic, so it
is a comparison basis and not a bill.

| Question | Answer from this evidence |
|---|---|
| Cheapest per request | GPT-4o mini at 0.094 USD / 1k requests |
| Cost of the best low-effort quality | GPT-5.6 Luna `none` at 0.324 USD / 1k, 3.5× GPT-4o mini |
| Cost of raising effort | GPT-5 mini `high` at 4.564 USD / 1k, 7.6× its own `minimal` |
| Cost of routing for quality | `quality` mode at 7.533 USD / 1k versus `cost` mode at 0.496 |
| Cost per conversation | 0.44 to 11.43 USD per 1,000 four-turn sessions depending on the arm |

<a id="configuration"></a>
## 5. Configuration

The settings the evidence supports are listed in the
[Executive Summary](#executive-summary). Two of them are easy to get wrong:

1. **Reasoning effort is not a quality dial on these tasks.** Every effort value that a
   model accepts was measured. Raising it increased tokens, latency and cost; the blind
   judge did not separate the results.
2. **A rate-limit fallback must watch the stream.** The reference policy that retries on
   HTTP 429 never triggered here, because the service answered 200 and then failed
   inside the stream. The working client switches backend on an error seen before any
   content is delivered.

<a id="reproducing"></a>
## 6. Reproducing

The offline path rebuilds every published number from the retained evidence and makes
no model call.

### Linux

```bash
git lfs version
git clone --filter=blob:none --sparse https://github.com/david-xinyuwei/david-share.git
git -C david-share sparse-checkout set Agents/Model-And-Router-Benchmark .github/workflows
git -C david-share lfs pull --include="Agents/Model-And-Router-Benchmark/**"
cd david-share/Agents/Model-And-Router-Benchmark
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --no-input -r requirements.txt
python scripts/validate_repo.py
```

### Windows PowerShell

```powershell
git lfs version
git clone --filter=blob:none --sparse https://github.com/david-xinyuwei/david-share.git
git -C david-share sparse-checkout set Agents/Model-And-Router-Benchmark .github/workflows
git -C david-share lfs pull --include="Agents/Model-And-Router-Benchmark/**"
Set-Location david-share\Agents\Model-And-Router-Benchmark
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --no-input -r requirements.txt
.\.venv\Scripts\python.exe scripts\validate_repo.py
```

**Done when** the last line reads
`PASS: 15/15 repository rules and all executable gates`. The command creates no cloud
resource; delete `.venv` to clean up.

**Running new measurements** costs money and needs your own Azure deployments. Each
study README carries its own live command. Put the benchmark VM in the same region as
the deployment, or the numbers will describe your network. The live console is
documented in [`live-benchmark-console`](live-benchmark-console/README.md); it binds to
loopback and expects authentication and a reverse proxy in front of it. This repository
publishes no hosted instance, endpoint or credential.

<a id="evidence"></a>
## 7. Evidence and boundaries

| Path | What it holds |
|---|---|
| [`scenario-model-benchmark/`](scenario-model-benchmark/) | Direct model × effort matrix, raw and full-text records, blind judge, report builder |
| [`model-router-validation/`](model-router-validation/) | Router modes, direct baselines, served-model traces, documentation gate |
| [`throughput-recalibration/`](throughput-recalibration/) | API-path comparison, stepped concurrency, judge rubric v2 |
| [`production-readiness/`](production-readiness/) | Multi-turn sessions, sustained load, rate-limit fallback, model lifecycle |
| [`live-benchmark-console/`](live-benchmark-console/) | Replay and live console, durable run history, same-region runner |
| [`scripts/`](scripts/) | Public-boundary redaction and the repository gate |
| [`evidence/`](evidence/) | De-identification manifest and executable rule results |

**What the evidence does not cover.** The prompts are synthetic and are not customer
production traffic. Quality is a blind model judge near the top of its scale, not a
human verdict. Concurrency beyond 16, windows beyond 90 s, and the rate-limit onset of
the large-capacity deployments were not measured. Three registry models were never
deployed and GPT-5.6 Sol was never swept across efforts; section 3.4 states that
boundary. Two prompt cells and one dependent
session carried identifiers and are withheld from this public copy; their numeric
fields, scores and response hashes are unchanged, so every aggregate still covers them.
Each study records the transformation in its `outputs/public_redaction.json`.

## Repository layout

```text
Model-And-Router-Benchmark/
├── scenario-model-benchmark/
├── model-router-validation/
├── throughput-recalibration/
├── production-readiness/
├── live-benchmark-console/
├── scripts/
├── tests/
└── evidence/
```
