# Speculative Decoding for OSS Model Deployment: MTP, DFlash 2 and Draft Model Adaptation

[![vLLM](https://img.shields.io/badge/vLLM-0.28.0-0078D4.svg)](https://github.com/vllm-project/vllm/releases/tag/v0.28.0)
[![GPU](https://img.shields.io/badge/GPU-H100%20NVL-76B900.svg?logo=nvidia&logoColor=white)](#test-method)
[![Precision](https://img.shields.io/badge/Precision-BF16-008080.svg)](#test-method)
[![Test scope](https://img.shields.io/badge/Scope-64%20tasks%20repeated-D97706.svg)](#coverage-and-unexecuted-work)
[![Evidence CI](https://github.com/david-xinyuwei/david-share/actions/workflows/speculative-decoding-ci.yml/badge.svg?branch=master)](https://github.com/david-xinyuwei/david-share/actions/workflows/speculative-decoding-ci.yml)

When you deploy an open-source model, deciding whether to enable speculative decoding — and which route to use — comes down to three questions:

- **Which route is available.** Does the target checkpoint ship MTP weights, or is a matching DFlash 2 draft model published separately? The prerequisites differ.
- **How much does it change.** On the same tasks and request settings, how far do throughput, latency and answer scores move, and at what cost.
- **What happens after fine-tuning.** Once the target is fine-tuned for your domain, can the released draft model still be used, or should the draft model and its selector receive continued training.

This repository answers all three on one H100 NVL with offline replay: inference comparison, target fine-tuning, continued training of the draft model and selector, checkpoint reload, held-out evaluation and vLLM serving checks.

> Author: Xinyu Wei (魏新宇)

[English](README.md) | [中文](README_CN.md)

[Choosing a Route](#choosing-between-mtp-and-dflash-2) · [Measured Comparison](#measured-comparison-mtp-and-dflash-2) · [After Fine-Tuning](#after-fine-tuning-adapting-the-draft-model) · [Quick Start](#quick-start) · [Tests](#tests-and-offline-replay)

Inference comparison: 2026-09-06, `qwen38-quality-20260906`; draft model adaptation: 2026-09-09 through 2026-09-10, with the setting B serving throughput re-tested on 2026-09-13 and swept across prompt blocks and concurrency levels on 2026-09-14. The Qwen3.6 experiment remains separate from both.

**Scope:** These are author-run experiments on one H100 NVL, not general quality or production guarantees. The selector objective is the author's implementation, not an official training recipe. Training starts from released draft weights; **from-scratch training has not been demonstrated**. The full-dataset inference stage was not run, and answer quality was not graded in the adaptation experiment. Setting B's serving throughput was re-measured four times on the same 40 prompts; the ranges bound timing jitter, not prompt-set sampling variance. It was then measured once on each of five disjoint 40-prompt blocks: the adapted draft model's gain over the released one averages +6% to +9% with per-block values from +2% to +14%, positive in all 20 block×concurrency cells; do not quote block 0, the high end, alone. Concurrency was measured up to 16, where the released draft model's speedup falls from 1.81× at concurrency 1 to 1.36×. The adaptation gain under thinking mode is unverified: one attempt returned no text to the client and is invalid. On vLLM 0.28.0 in bf16, greedy speculative output is not byte-identical to greedy autoregressive output: 20–24 of every 40 responses match across the five blocks, and the divergence reproduces deterministically. The publisher's model card states that "greedy output matches the target model exactly"; that was not observed on this engine, and whether the cause is the algorithm or the engine's numeric path was not isolated.

---

## Start Here

| What you want | Go to |
|---|---|
| Choose between MTP and DFlash 2 | [Choosing Between MTP and DFlash 2](#choosing-between-mtp-and-dflash-2) |
| Understand delivered assets and responsibilities | [What This Repository Delivers](#what-this-repository-delivers) |
| Inspect raw throughput, latency and score data | [Measured Comparison: MTP and DFlash 2](#measured-comparison-mtp-and-dflash-2) |
| Understand draft model and selector training and their losses | [Loss Functions and the Selector](#loss-functions-and-the-selector) |
| Assess adaptation results | [Held-Out and Serving Evaluation](#held-out-and-serving-evaluation) |
| Start inference or adaptation on your own GPU | [Quick Start](#quick-start), [Reproducing the Adaptation](#reproducing-the-adaptation) |
| Inspect logs, code and saved results without a GPU | [Training Logs and Code](#training-logs-and-code), [Tests and Offline Replay](#tests-and-offline-replay) |
| Check compatibility, from-scratch scope and earlier failures | [Compatibility and Limits](#compatibility-and-limits) |

## Choosing Between MTP and DFlash 2

### 1. Determine which route is available

| Your situation | Route | Prerequisite |
|---|---|---|
| The target checkpoint ships MTP weights | MTP | No extra weight file; enable the serving flag |
| Upstream published a matching DFlash 2 draft model | DFlash 2 | Download the roughly 3.8 GB draft model; architecture and tokenizer must match the target |
| Both are available | Test both | Run your own workload through each and read throughput and answer quality together |
| Neither is available | Do not enable speculative decoding yet | Training a new draft model is a separate project; see [What Training From Scratch Would Take](#what-training-from-scratch-would-take) |

Enabling a serving flag does not create missing MTP weights. A DFlash 2 draft model only fits the target it was published for; changing the target requires rechecking architecture, tokenizer, hidden size and output head. Full launch commands for all three routes are in [Quick Start](#quick-start).

### 2. Measured Differences Between the Two Routes

<!-- BEGIN DECISION_TABLE -->
| Comparison | MTP7 | DFlash 2-7 |
| --- | --- | --- |
| Output tok/s (concurrency 1 / 4 / 8) | 113.3 / 382.9 / 565.2 | 150.5 / 451.7 / 741.1 |
| Speedup over no speculation (concurrency 1 / 4 / 8) | 2.12× / 2.01× / 1.96× | 2.82× / 2.38× / 2.58× |
| Time per output token, ms (concurrency 1 / 4 / 8) | 8.01 / 8.55 / 10.04 | 5.87 / 6.74 / 7.89 |
| Code score across 9 paired groups | Reference | 2 higher / 2 lower / 5 tied |
| Math score across 9 paired groups | Reference | 6 higher / 2 lower / 1 tied |

Throughput and latency are medians across three seeds. Scores are compared per matched concurrency and seed; repeats are not independent tasks.
<!-- END DECISION_TABLE -->

**Speed reads directly; accuracy does not.** DFlash 2 produced higher throughput than MTP7 in all nine matched groups, so that direction is consistent. Answer scores moved in both directions with no systematic decline, but each dataset has only 32 distinct tasks repeated three times, which is not enough to establish non-inferiority. Treating the score differences as noise, or as quality regression, is equally unsupported today.

**Faster tokens do not mean faster correct answers.** In one concurrency-4 run, DFlash 2 emitted tokens faster yet finished the same task group more slowly, and its normal-stop correct-answer rate was also lower than MTP7's. The full data for this counterexample is in [Measured Comparison](#measured-comparison-mtp-and-dflash-2).

In practice, treat DFlash 2 as a throughput candidate: retest answer quality on your own tasks and acceptance criteria before deploying, rather than citing the scores here as an acceptance result.

### 3. Adjusting the Draft Model After the Target Is Fine-Tuned

A draft model is trained against the target's hidden features, so fine-tuning the target can shift its predictions. This repository measured two fine-tuning settings:

| Your fine-tuning | Recommended action | Measured basis |
|---|---|---|
| Any fine-tuning | **Test the released draft model first; do not retrain by default** | In setting A all three seeds lowered training loss, yet paired agreement did not improve |
| Light LoRA (attention projections only, low rank) | The released draft model is likely reusable | Setting A: rank 16, attention only, 1 epoch; retraining produced no measurable gain |
| Heavy LoRA (all projections, high rank, different language) | A single adaptation pass is worth trying | Setting B: rank 128, 7 projection modules, Chinese corpus; retraining raised first-offset agreement and serving throughput; serving throughput exceeds the released draft model on all 5 independent prompt blocks × concurrency 1/4/8/16, mean gain +6% to +9%, per-block +2% to +14% |
| Either way | Accept on your own held-out set and answer-quality criteria | This experiment measured agreement and throughput only, and did not grade answer quality |

**These two settings are not a clean intensity control.** Setting A is an English attention LoRA and setting B is a Chinese all-module LoRA, so language and parameters differ at the same time. The table is a starting point for selection, not evidence that heavier fine-tuning always requires adaptation.

**Do not accept on training loss.** Setting A's loss did fall while agreement and serving throughput did not improve. The full flow, loss functions and per-item numbers are in [After Fine-Tuning: Adapting the Draft Model](#after-fine-tuning-adapting-the-draft-model).

## What This Repository Delivers

| Goal | Provided assets | Practical benefit |
|---|---|---|
| Start all three inference routes | Pinned weights, complete launch commands and identical request examples | Avoid assembling MTP, DFlash and client settings from scratch |
| Select a route for further evaluation | Throughput, latency, correct counts and length stops on the same tasks | Compare speed and quality together, including cases where faster tokens do not deliver correct answers sooner |
| Adapt a draft model | Target LoRA, self-generated corpus, draft training, checkpoint reload and serving export | Compare keeping the released draft model with retraining instead of assuming retraining is necessary |
| Train the selector | `selector_loss()`, `--train-selector`, training histories and executed code | Understand candidate rescoring and gradient boundaries without attributing joint-training gains to one component |
| Reuse the training method | Fixed splits, frozen targets, same-text pairing, separate loss histories and serving rechecks | Do not replace answer-quality acceptance with lower training loss or higher draft agreement |
| Check the selection evidence | Per-group records, grader integration, analysis and tests | Trace the reported numbers and design acceptance tests for your own workload |

This is a deployment reference and test evidence, not a production-validated hosted service. Preparation and scheduling for the full 27-group experiment do not yet have a standalone public entry point; see [reproduction scope](#reproduction-scope).

MTP, DFlash and the released checkpoints are upstream work. This repository contributes training and measurement implementations, controlled comparisons and traceable evidence. Customers supply compatible hardware, workload data and answer-quality criteria. Trained weights are not redistributed; code and loss histories are inspectable.

## Measured Comparison: MTP and DFlash 2

This experiment asks whether changing the drafting route improves serving performance and changes answer scores while holding target weights, tasks and requests fixed. Inputs are 32 HumanEval+ tasks and 32 MATH-500 tasks, not free-form chat load.

A verbatim example from the actual `HumanEval/69` request:

> `search([4, 1, 2, 2, 3, 1]) == 2`

The `MATH-500/100` request begins "A hexagon is inscribed in a circle:". Both [full requests](experiments/20260906-qwen38/evidence/request-examples.json) retain problem statements, diagram descriptions, sampling settings and hashes. The comparison switches baseline, MTP7 and DFlash 2-7 without changing output budgets or grading methods.

### What the Current Run Shows

The same 32 code and 32 math tasks were run at three concurrency levels with three seeds each. Against MTP7, DFlash 2's code correct count is higher in 2 groups, lower in 2 and tied in 5; math is higher in 6, lower in 2 and tied in 1. The baseline's three code runs at concurrency 1 score 29, 31 and 30. This shows variation across repeats, not that a difference in another group is caused by sampling noise.

| Question | Observation |
|---|---|
| Was output throughput higher? | Yes. DFlash 2 exceeded MTP7 in all nine matched concurrency/seed pairs |
| Was non-decreasing accuracy proved? | No. Each dataset has 32 distinct tasks; three repeats measure variation, not 96 independent tasks |
| Were correct answers always delivered faster? | No. At concurrency 4 in the third run, DFlash 2 took longer for the same group and delivered fewer normal-stop-correct answers per second than MTP7 |
| Was the planned full dataset tested? | No. There were 1,920 completed responses out of 5,904 planned; the remaining 3,984 were not executed and are not counted as incorrect |

A throughput advantage does not replace accuracy, latency and error-rate acceptance on customer workloads.

### Test Method

The three routes keep the target model, precision, tasks, output budget and sampling fixed while switching speculative configuration. Settings come from the [saved configuration](experiments/20260906-qwen38/evidence/configuration.json); observed loading checks are in `activation` in the [run evidence](experiments/20260906-qwen38/evidence/run.json).

See [architecture and test setup](#architecture-and-test-setup) for the client, inference service and graders. Client and server share one host. Timing covers request dispatch through streamed response completion, excluding model startup and offline grading.

| Setting | This run |
|---|---|
| Hardware | One H100 NVL; tensor parallelism 1 |
| Target | Qwen3.8-27B, BF16 |
| MTP | Native weights in the target checkpoint; not separately trained or converted |
| DFlash 2 | incoai's Qwen3.8-27B-DFlash2, BF16 |
| Engine | vLLM 0.28.0; Model Runner V2 observed loading |
| Candidates | Baseline: 0; MTP7 and DFlash 2-7: 7 each |
| Output budget | At most 16,384 tokens per task, including thinking |
| Thinking | Enabled and preserved; `reasoning_effort="xhigh"` |
| Sampling | temperature 1.0, top_p 0.95, top_k 20 |
| Pairing | Concurrency 1, 4, 8; seeds 20260906, 20260907, 20260908 |

The 32 task IDs per dataset were selected by a frozen SHA-256 ordering rule, independently of answers and scores. Routes share per-task seeds, which does not imply aligned random draws at every token. Code was graded by the official EvalPlus tools; math by the official Math-Verify tool.

#### Pinned Versions, Full Parameters and Request Examples

These links identify the actual versions used, not current model repository heads:

| Component | Pinned record |
|---|---|
| Target | [Qwen3.8-27B checkpoint](https://huggingface.co/Qwen/Qwen3.8-27B/tree/1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0) |
| DFlash 2 | [Qwen3.8-27B-DFlash2 checkpoint](https://huggingface.co/incoai/Qwen3.8-27B-DFlash2/tree/dedf8df68adfb1afeaf7b7480c0a0243108177b4), architecture `DFlash2DraftModel` |
| vLLM | [0.28.0 source](https://github.com/vllm-project/vllm/tree/2cf0a6915ce544dc493a0990f2ea38d81601128a) |
| EvalPlus | [Pinned source](https://github.com/evalplus/evalplus/tree/26d6d00bb1fd0fa37f39c99d5290da67891d1c5e), official sanitize/evaluate CLI |
| Math-Verify | [Pinned source](https://github.com/huggingface/Math-Verify/tree/ba3d3aaff23b3f4cac7a14672b4f6e293d97c98b), official `evaluate_model_outputs.py` |

Remaining settings: `min_p=0.0`, `presence_penalty=0.0`, `repetition_penalty=1.0`. The template sets `enable_thinking=true` and `preserve_thinking=true`. Scheduling limits are `max_model_len=32768`, `max_num_seqs=16`, `max_num_batched_tokens=16384`.

Client and server run on the same machine, with closed-loop fixed-concurrency dispatch over loopback in the frozen request order. Equal client concurrency does not imply identical GPU batch shapes. Archived route labels are `baseline`, `mtp7`, `dflash2_7`; the base configuration seed is 20260906.

[Request examples](experiments/20260906-qwen38/evidence/request-examples.json) retain prompts, full payloads and hashes for `HumanEval/69` and `MATH-500/100` from the first baseline group. `raw_correct` records official correct verdicts; `normal_correct` also requires `finish_reason=stop`. They happen to agree in this S stage. Offline analysis uses saved grades and does not regrade answers.

### Throughput and Answer Quality

Three routes used the same 32 HumanEval+ code tasks and 32 MATH-500 tasks at concurrency 1, 4 and 8, with three seeds each: 27 groups. **Repeating 32 tasks three times does not create 96 independent tasks per dataset.**

The baseline has no speculation; MTP7 and DFlash 2-7 each use seven draft tokens. Throughput and group wall time are separately summarized by their three-run medians. Triples in the score and length-stop tables are ordered by seed **20260906, 20260907, 20260908**. Each score is out of 32.

![Output throughput at concurrency 1, 4 and 8](experiments/20260906-qwen38/images/throughput.png)

*Figure 1. Author's measurements. Bars show medians of three runs; whiskers show observed minima and maxima, not confidence intervals. The same 64 tasks are used throughout; throughput includes thinking tokens. Source: [group records](experiments/20260906-qwen38/data/groups.json).*

<!-- BEGIN RESULT_TABLE -->
#### Throughput and Group Duration

| Concurrency | Route | Output tok/s | Group wall (s) |
| --- | --- | --- | --- |
| 1 | Baseline | 53.40 | 3458.61 |
| 1 | MTP7 | 113.34 | 1591.09 |
| 1 | DFlash 2-7 | 150.51 | 1149.41 |
| 4 | Baseline | 190.08 | 1037.22 |
| 4 | MTP7 | 382.85 | 470.35 |
| 4 | DFlash 2-7 | 451.74 | 377.79 |
| 8 | Baseline | 287.72 | 577.44 |
| 8 | MTP7 | 565.24 | 308.08 |
| 8 | DFlash 2-7 | 741.07 | 249.68 |

#### Code and Math Scores

| Concurrency | Route | Code correct /32 | Math correct /32 |
| --- | --- | --- | --- |
| 1 | Baseline | 29, 31, 30 | 30, 30, 30 |
| 1 | MTP7 | 30, 31, 31 | 30, 28, 31 |
| 1 | DFlash 2-7 | 30, 31, 30 | 29, 32, 30 |
| 4 | Baseline | 31, 31, 31 | 30, 31, 30 |
| 4 | MTP7 | 30, 30, 31 | 29, 29, 29 |
| 4 | DFlash 2-7 | 31, 31, 29 | 31, 31, 30 |
| 8 | Baseline | 31, 31, 31 | 30, 29, 30 |
| 8 | MTP7 | 31, 31, 30 | 29, 29, 30 |
| 8 | DFlash 2-7 | 31, 31, 30 | 30, 31, 30 |

All answers marked correct by the graders stopped normally in this run, so raw-correct and normal-stop-correct counts coincide. Both fields remain in the data; duplicate columns are omitted here.

#### Length Stops Across the Three Runs

| Concurrency | Route | Code length stops | Math length stops |
| --- | --- | --- | --- |
| 1 | Baseline | 1, 1, 2 | 2, 1, 2 |
| 1 | MTP7 | 2, 1, 1 | 1, 2, 1 |
| 1 | DFlash 2-7 | 2, 1, 2 | 2, 0, 1 |
| 4 | Baseline | 1, 1, 1 | 1, 0, 2 |
| 4 | MTP7 | 2, 2, 1 | 2, 1, 2 |
| 4 | DFlash 2-7 | 1, 1, 3 | 1, 1, 1 |
| 8 | Baseline | 1, 1, 1 | 2, 2, 2 |
| 8 | MTP7 | 1, 1, 2 | 2, 1, 1 |
| 8 | DFlash 2-7 | 1, 1, 2 | 2, 1, 2 |

Length-stopped responses remain in each 32-task denominator.
<!-- END RESULT_TABLE -->

These tables correspond to the [saved summary](experiments/20260906-qwen38/data/summary.json). Throughput is server-confirmed output tokens divided by entire-group wall time, **including thinking, incorrect and length-stopped responses**. Timing runs from the first measured request dispatch to the last request's terminal event; it excludes model download, startup, warmup and grading. This is not raw GPU decode throughput.

#### Why Correct-Answer Delivery Also Matters

<!-- BEGIN COUNTEREXAMPLE -->
**Observed counterexample: concurrency 4, seed 20260908. This table uses that individual run, not the three-run medians above.**

| Route | This run's group wall (s) | Normal-correct code /32 | Normal-correct math /32 |
| --- | --- | --- | --- |
| MTP7 | 450.87 | 31 | 29 |
| DFlash 2-7 | 484.07 | 29 | 30 |

DFlash 2 has 3 length-stopped code responses. Using each dataset's `normal_correct / entire group wall time`, the DFlash/MTP normal-correct answer-rate ratios are **0.8713 for code** and **0.9635 for math**. Both are below 1 despite the higher DFlash token rate in this run. This is not a causal diagnosis or evidence that every performance metric improved.
<!-- END COUNTEREXAMPLE -->

### Client Latency

TTFT is the wait for the first output token; TPOT is the average delivery interval per output token after the first; response time runs from request dispatch to the terminal event. All three are observed at the client, and lower is better.

Each configuration first computes the P50 of each of its three runs, then takes the median of those three P50 values. **Responses are not pooled into one percentile.** Missing or undefined values are not filled with zero.

<!-- BEGIN LATENCY_TABLE -->
#### Time to First Token (ms)

| Concurrency | Baseline | MTP7 | DFlash 2-7 |
| --- | --- | --- | --- |
| 1 | 82.727 | 75.127 | 80.286 |
| 4 | 104.008 | 110.259 | 115.994 |
| 8 | 106.598 | 124.699 | 124.781 |

#### Time per Output Token (ms/token)

| Concurrency | Baseline | MTP7 | DFlash 2-7 |
| --- | --- | --- | --- |
| 1 | 18.567 | 8.011 | 5.875 |
| 4 | 20.104 | 8.549 | 6.739 |
| 8 | 20.845 | 10.041 | 7.892 |

#### Response Time (s)

| Concurrency | Baseline | MTP7 | DFlash 2-7 |
| --- | --- | --- | --- |
| 1 | 16.359 | 6.236 | 6.185 |
| 4 | 19.035 | 8.661 | 5.237 |
| 8 | 18.457 | 9.915 | 7.742 |

Each metric in each configuration has 192 valid response observations and 0 missing observations. These are repeated responses, not independent tasks.
<!-- END LATENCY_TABLE -->

#### Exact Latency Definitions

TTFT is timed from request dispatch to the first non-empty generated `token_ids` event; empty role or usage events do not count as the first token. TPOT is `(last_token_time - first_token_time) / (completion_tokens - 1)`, defined only when more than one token was produced and the token-ID coverage check passed.

A speculative-decoding SSE chunk can carry several tokens, so these are client-side receive metrics, not GPU kernel times. Definitions are in the [configuration](experiments/20260906-qwen38/evidence/configuration.json); observations are in the [group records](experiments/20260906-qwen38/data/groups.json).

### Coverage and Unexecuted Work

The original plan has four stages. Performance and score tables in this report use only S. Compatibility and greedy diagnostics are not pooled into the formal subset comparison.

| Stage | Completed groups | Responses | Status |
|---|---:|---:|---|
| C: compatibility | 6 | 48 | Complete |
| G: greedy diagnostics | 36 | 144 | Complete |
| S: repeated subset | 27 | 1,728 | Complete |
| F: full datasets | 0 | 0 | Not run |

Totals are **69/81 groups and 1,920/5,904 responses**. The remaining 12 groups and 3,984 responses were not executed: neither removed from the plan nor marked incorrect. These results cover the completed work, not a pass for the entire plan.

F would run all three routes at concurrency 1 and 8 over all 164 HumanEval+ and 500 MATH-500 tasks, once per task, with seed 20260906. This stage was not executed. The measured subset is not a full-dataset score; the [coverage record](experiments/20260906-qwen38/evidence/run.json) preserves the original plan and unexecuted items.

#### Measured Duration by Stage

| Stage | Sum of group wall times (s) |
|---|---:|
| Compatibility (C) | 928.51 |
| Greedy diagnostics (G) | 387.09 |
| Repeated subset (S) | 27,800.41 |
| Full datasets (F) | 0, not run |

These sum measured group times from request dispatch to response completion, excluding model downloads, server startup, warmup and grading. Values are rounded to two decimals; exact values remain in [run evidence](experiments/20260906-qwen38/evidence/run.json). Complete describes execution, not perfect correctness.

## After Fine-Tuning: Adapting the Draft Model

**Does the released DFlash 2 draft model remain useful after target fine-tuning, and does retraining the draft model and selector help?** This experiment compares English attention-only LoRA (setting A) and Chinese all-module LoRA (setting B) on one H100 NVL. Language and adapter parameters both change; these settings do not isolate fine-tuning strength.

### Data and Training Flow

Both settings take a fixed split of 2,000 target-training questions and 200 held-out prompts from [medical-o1-reasoning-SFT](https://huggingface.co/datasets/FreedomIntelligence/medical-o1-reasoning-SFT). The first English held-out prompt begins verbatim:

> A 65-year-old woman presents to her family doctor to reestablish care since her retirement from her corporate job and loss of her employer-sponsored health insurance.

Full inputs are in the [English prompts](experiments/20260909-drafter-adaptation/inputs/round4/eval_prompts_en200.jsonl) and [Chinese prompts](experiments/20260909-drafter-adaptation/inputs/round4/eval_prompts_zh200.jsonl). They are public-dataset questions, not customer patient records. Medical answer quality was not graded here.

The target is `Qwen/Qwen3.8-27B` (revision `1d4bf0f2`) with a LoRA adapter. Setting A uses English data, rank 16, attention projections and one epoch. Setting B uses Chinese data, rank 128, all seven projection modules and two epochs.

Draft training starts from `incoai/Qwen3.8-27B-DFlash2` (revision `dedf8df6`), updating its five draft layers and selector while the target, embedding and output head remain frozen. The adapted target generates responses for 1,200 training questions. After the script's usable-length filtering, 1,160 English sequences and 1,199 Chinese sequences enter two epochs of training. This is continuation of released weights, not from-scratch training.

### Loss Functions and the Selector

Training has two stages. Target-LoRA loss and draft loss must not be presented as one curve.

- **Target LoRA:** `DomainDataset` in [finetune_target.py](experiments/20260909-drafter-adaptation/source/round4/finetune_target.py) masks prompt labels with `-100`. `out.loss` supervises only answer and end tokens while LoRA parameters are updated.
- **Draft backbone:** freeze the fine-tuned target, its embedding and output head. `anchor_loss()` computes cross-entropy over seven predicted positions per block, weighting earlier positions more heavily. This run records `block=8` and `gamma=7.0`.
- **Selector:** `selector_loss()` trains on the backbone's top-k candidates using the true predecessor token and pairwise successor scores. A position contributes only if its true token is in the top-k set. Hidden states and logits are detached, so this objective updates only the selector; backbone loss still updates the draft backbone.

For predicted positions $k=1,\ldots,7$, the executed backbone objective is:

$$
w_k=\exp\left(-\frac{k-1}{7}\right),\qquad
L_{\mathrm{draft}}=\frac{\sum_{k=1}^{7}w_k\,\mathrm{CE}(z_k,y_k)}{\sum_{k=1}^{7}w_k}.
$$

The selector scores candidate $c$ as $s_{k,c}=z_{k,c}+\langle E_{\mathrm{prev}}(y_{k-1})\odot Ph_k,E_{\mathrm{next}}(c)\rangle$. Its cross-entropy is averaged over eligible positions with the same normalized weights $w_k$. A sample's objective averages $L_{\mathrm{draft}}+1.0\,L_{\mathrm{selector}}$ over its anchors. `selector_weight=1.0` is a recorded experiment parameter, not an upstream constant.

The following excerpt is verbatim from [train_drafter.py](experiments/20260909-drafter-adaptation/source/round4/train_drafter.py), not a standalone program:

```python
    per_token = nn.functional.cross_entropy(logits[0].float(), labels, reduction="none")
    backbone = (per_token * weights).sum() / weights.sum()
    if not train_selector:
        return backbone, None
    selector = selector_loss(drafter, hidden.detach(), logits.detach(), input_ids, anchor, block,
                             weights, autocast)
    return backbone, selector
```

Candidate rescoring uses these actual lines:

```python
        pairwise = torch.einsum("pr,pkr->pk", pred_emb * projected[0], succ_emb)
        scores = unary[0].float() + pairwise.float()                              # [B-1, k]
```

This is the author's selector objective, not the official DFlash 2 training recipe. The snapshot's module header retains an obsolete statement that the selector is not trained; the later `selector_loss()` implementation, `--train-selector` flag and saved arguments identify the executed behavior. Archived source is unchanged.

### Training Logs and Code

All four draft-training runs preserve per-step backbone and selector losses. This table is generated from the full histories, comparing the first and last 10% of steps with `max(1, steps // 10)` as the window. Loss trends describe optimization, not held-out quality or serving performance.

![Backbone and selector losses from four draft-training runs](experiments/20260909-drafter-adaptation/images/training-loss-en.png)

*Author's measurements, 2026-09-09 through 2026-09-10, H100 NVL, two epochs. Three English runs have 2,320 steps each; the Chinese run has 2,398. Curves show consecutive 50-step means with the final partial group retained, not confidence intervals. Source and image hashes are in [loss-figures.json](experiments/20260909-drafter-adaptation/images/loss-figures.json); the [figure generator](tools/make_readme_figures.py) reads the complete histories. Absolute losses across languages and targets are not a ranking.*

<!-- BEGIN TRAINING_LOSS -->
| Training run | Steps / window | Backbone loss: first / last | Selector loss: first / last |
| --- | --- | --- | --- |
| [English seed 20260908](experiments/20260909-drafter-adaptation/results/round3/training/drafter_v3_history.json) | 2320 / 232 | 2.0278 / 1.5448 | 0.8209 / 0.6064 |
| [English seed 1](experiments/20260909-drafter-adaptation/results/round4/training/drafter_v3_seed1_history.json) | 2320 / 232 | 2.0318 / 1.5081 | 0.8103 / 0.5854 |
| [English seed 2](experiments/20260909-drafter-adaptation/results/round4/training/drafter_v3_seed2_history.json) | 2320 / 232 | 2.0126 / 1.5282 | 0.7989 / 0.6043 |
| [Chinese seed 20260908](experiments/20260909-drafter-adaptation/results/round4/training/drafter_zh_history.json) | 2398 / 239 | 3.2205 / 2.2704 | 1.4236 / 1.0523 |
<!-- END TRAINING_LOSS -->

Target LoRA has separate [English loss records](experiments/20260909-drafter-adaptation/results/round3/training/adapter_v2_summary.json) and [Chinese loss records](experiments/20260909-drafter-adaptation/results/round4/training/adapter_zh_summary.json). Readable [Round 3 logs](experiments/20260909-drafter-adaptation/logs/round3/) and [Round 4 logs](experiments/20260909-drafter-adaptation/logs/round4/) retain training metrics, stage/terminal markers and server counters. [provenance.json](experiments/20260909-drafter-adaptation/evidence/provenance.json) binds source hashes, retained line numbers and public-file hashes. These are explicitly excerpted public projections; full raw logs and orchestration scripts containing private paths remain in the author's archive.

### Held-Out and Serving Evaluation

**Same-text draft comparison:** both draft models read identical cached target answers. First-offset agreement checks the first prediction in each block; joint-prefix acceptance length is one plus the mean leading-correct run. The fixed eight-token anchor grid differs from runtime re-anchoring after rejection, so these are teacher-forced metrics. Only matching cache and text hashes permit a paired bootstrap.

**Runtime checks:** the HF reference decoder and vLLM each perform real drafting and verification. End-to-end acceptance, server accepted/drafted counters and client throughput remain separate measurements, none a substitute for answer grading.

<!-- BEGIN ADAPTATION_TABLE -->
#### First-Offset Agreement

| Setting / draft path | Released / adapted | 95% interval of difference | Paired prompts |
| --- | --- | --- | --- |
| A / seed 20260908 | 0.857 / 0.848 | -0.019, +0.002 | 193 |
| A / seed 1 | 0.857 / 0.845 | -0.024, +0.000 | 193 |
| A / seed 2 | 0.857 / 0.840 | -0.029, -0.005 | 193 |
| B / selector | 0.677 / 0.707 | +0.015, +0.044 | 200 |
| B / argmax | 0.685 / 0.718 | +0.019, +0.048 | 200 |

#### Joint-Prefix Acceptance Length

| Setting / draft path | Released / adapted | 95% interval of difference | Paired prompts |
| --- | --- | --- | --- |
| A / seed 20260908 | 4.33 / 4.33 | -0.056, +0.069 | 193 |
| A / seed 1 | 4.33 / 4.35 | -0.044, +0.088 | 193 |
| A / seed 2 | 4.33 / 4.35 | -0.042, +0.095 | 193 |
| B / selector | 2.85 / 3.08 | +0.163, +0.295 | 200 |
| B / argmax | 2.78 / 3.00 | +0.157, +0.274 | 200 |

Each language requested 200 prompts; the table shows evaluable pairs, with short-output exclusions retained in the records. A uses the selector. B's argmax row disables it on the same trained weights; this is not a training ablation. Five comparisons with two metrics give ten intervals, using 2,000 prompt-level bootstrap resamples without multiplicity correction.

#### vLLM Serving Measurements

Each fine-tuned target is served without speculation, with the released draft model and with the adapted draft model, once per route. Each concurrency level measures 40 prompts with `max_tokens=256`. A's serving run covers only seed 20260908; the other two seeds were not serving-tested.

| Metric | A / en | B / zh |
| --- | --- | --- |
| Throughput tok/s, concurrency 1 | 53.5 / 162.2 / 158.0 | 53.6 / 97.1 / 106.0 |
| Throughput tok/s, concurrency 4 | 184.7 / 490.0 / 485.3 | 194.5 / 323.6 / 349.4 |
| Server acceptance length | 4.19 / 4.06 | 2.39 / 2.61 |

Throughput cells list no speculation / released / adapted; acceptance cells list released / adapted. Acceptance is derived from cumulative logged accepted/drafted counts, including warmup and both concurrency levels, so its denominator differs. No serving-significance claim is made; answer quality was not graded.

#### Setting B Serving Re-test (Round 5)

On 2026-09-13 the three serving routes of setting B were re-run in a fresh VM session with a fresh vLLM install: same weights, same 40 prompts, same seed and engine flags. Each route started the server twice (pass A ordered no speculation → released → adapted, pass B reversed) and ran the client twice per server, giving four throughput observations per cell. The table reports means with raw ranges and makes no distributional assumption.

| Concurrency | No speculation | Released | Adapted | Adapted / released | Ranges overlap |
| --- | --- | --- | --- | --- | --- |
| 1 | 53.2 [53.0–53.6] | 96.6 [96.3–97.0] | 105.4 [105.1–105.8] | +9.1% | no |
| 4 | 194.4 [194.2–194.7] | 323.9 [323.5–324.4] | 349.1 [348.7–349.4] | +7.8% | no |
| 8 | 336.3 [335.5–337.5] | 531.9 [529.9–533.5] | 577.1 [575.3–578.5] | +8.5% | no |

Throughput in tok/s; each cell is mean [min–max] over 4 observations. The observations are timing repeats of the same deterministic greedy decode, so the ranges bound measurement jitter, not prompt-set sampling variance; the 40 prompts remain a single sample. Byte identity at concurrency 1 (by `text_sha256`): released and adapted draft models produce 40/40 identical outputs in every run; no-speculation and released speculative decoding agree on 21/40; the same route across the two server starts agrees on 40/40, 40/40, 40/40. Concurrency 4 and 8 counts are in `round5.text_identity` of the [summary](experiments/20260909-drafter-adaptation/data/summary.json).

#### Prompt-Set Variance and Higher Concurrency (Round 6)

Round 5's ranges cover timing jitter only. To see whether the gain survives a different prompt sample, on 2026-09-14 the same 200 held-out Chinese prompts were split in file order into 5 blocks of 40 (block 0 is the Round 4/5 set), each serving route started one server, and every block was measured once at concurrency 1/4/8/16. Each cell below is the adapted-over-released throughput gain on that block; the mean and minimum are computed from the five numbers with no distributional assumption.

| Concurrency | b0 | b1 | b2 | b3 | b4 | Mean | Min | All 5 positive |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | +9.0% | +9.4% | +8.9% | +5.3% | +6.0% | +7.7% | +5.3% | yes |
| 4 | +7.7% | +10.8% | +10.5% | +6.2% | +5.5% | +8.1% | +5.5% | yes |
| 8 | +8.7% | +2.1% | +8.6% | +3.9% | +7.0% | +6.1% | +2.1% | yes |
| 16 | +13.6% | +8.7% | +12.5% | +8.1% | +3.0% | +9.2% | +3.0% | yes |

| Route | c1 | c4 | c8 | c16 |
| --- | --- | --- | --- | --- |
| Released / no speculation | 1.81× | 1.68× | 1.53× | 1.36× |
| Adapted / no speculation | 1.96× | 1.81× | 1.62× | 1.49× |

The gain is positive in all 20 block×concurrency cells, but its size depends on the prompt block: block 0, the Round 4/5 set, sits at the high end, and other blocks drop to 2.1%. Quote the mean and per-block range rather than block 0 alone. The second table shows five-block mean speedups: the released draft model loses about 25% of its speedup from concurrency 1 to 16, and the adapted model's relative gain stays positive at 16. Concurrency 16 equals the engine's `max-num-seqs`. Block 0 deviates from the Round 5 four-run means by at most 0.66%. At concurrency 1 the byte-identical counts between no-speculation and released speculative decoding are 21/24/20/21/21 per block (40 prompts each), and between released and adapted 40/40/40/40/40.

A block-0 attempt with `enable_thinking=true` and `max_tokens=2048` (concurrency 4/8) was also made the same day. The server generated and billed roughly 130 tokens per response, but the client received empty `content` and zero `reasoning_content` characters for every response; the mode the model actually ran in cannot be confirmed, and any byte-identity count would compare empty strings. The files are published under [results/round6/vllm/](experiments/20260909-drafter-adaptation/results/round6/vllm/) but **support no conclusion about thinking mode**; the adaptation gain under thinking remains unverified.
<!-- END ADAPTATION_TABLE -->

**Setting A:** paired measurements show no draft-agreement improvement across the three training seeds. Only seed 20260908 was compared in vLLM, with no observed throughput gain. There is no matching base-target measurement on these English prompts, so whether fine-tuning harmed the released draft model is not established.

**Setting B:** same-text pairing shows higher draft agreement and joint-prefix acceptance, alongside higher vLLM server counters and throughput. This is a joint-training result; the selector training contribution was not isolated. Round 5 re-ran the three serving routes four times each in a fresh session: the adapted draft model exceeds the released one by 9.1% / 7.8% / 8.5% at concurrency 1/4/8 with non-overlapping ranges at every level, within 0.25 percentage points of the single Round 4 measurements (+9.2% / +8.0%). The two sessions share weights, prompts, seed and engine flags, and deterministic greedy decoding forces them to agree, so this is cross-session reproducibility rather than independent replication. At concurrency 1 the two draft models produce byte-identical output on 40/40 prompts: every accepted token is the target model's own argmax, so the draft model changes the number of verification steps, not the output. Swapping draft models should not be expected to change answers. Round 6 then split the 200 prompts into 5 blocks and measured each once: the gain is positive in all 20 block×concurrency cells, but the 40 prompts used in Rounds 4/5 sit at the high end; the five-block mean is +6% to +9% and the lowest single block is +2.1%. **Quote this mean and range externally, not Round 5's +9%.**

The base-target comparison is a separate cross-text diagnostic. The released draft model scores 0.720 first-offset agreement and 3.24 joint-prefix length on base-target outputs. Those outputs differ from the fine-tuned target's, so this comparison does not define a paired recovery fraction. In the 40-output screen, base answers average 252 tokens with 39 reaching the 256-token cap; fine-tuned answers average 119. Token-level repeated 4-gram fractions are 0.054 and 0.021 respectively. These are confounders, not demonstrated causes of the agreement difference.

**The opposing result remains.** In setting B, one 40-prompt run of the HF reference path `dflash_generate` gives end-to-end acceptance length 3.12 for the released draft model and 2.91 for the adapted one, opposite to the paired measurements and vLLM results. None of the 40 completions is byte-identical across draft models. That prevents same-text pairing; it does not justify discarding the result.

There is also a cross-engine acceptance gap between HF and vLLM. Batching, precision and cache paths were not aligned between engines, and the cause remains uninvestigated.

**Scope:** one target family, one dataset family and one GPU. The author selected these settings; they are not calibrated retraining thresholds.

The Chinese fine-tuned target failed the token-repetition screen: 11 of 40 responses contain a 4-gram at least three times, against a two-response limit. After the pipeline stopped, the author found 38 repetition failures and 39 length stops on the base target, then continued with a quality warning. Both failing does not establish that the screen is invalid or either target's answers are acceptable. A later review of the per-response records ([gates/target_zh.json](experiments/20260909-drafter-adaptation/results/round4/gates/target_zh.json)) found the largest `max_4gram_count` among the 11 flagged responses is 4, the `repeat_4gram` median across 40 responses is 0.009 with a maximum of 0.088, against a maximum of 0.541 on the English target that Rounds 1–2 confirmed as degenerate; the repeated units are Chinese medical stock phrases and parallel clauses such as "according to the patient's" and "the most likely". The `>= 3` limit is a bare constant introduced during the English experiments with no calibration record, and four tokens cover roughly 2–4 Chinese characters, so the rule does not transfer across languages. The author's judgement is that this failure was a screen false positive; that is a judgement about the repetition metric, still not an answer grade.

[Inputs and split manifests](experiments/20260909-drafter-adaptation/inputs/) are published and checked against run hashes. The dataset revision was not pinned; a new download must pass the hash checks. Round 3 regenerated target text per run and stored only marginal agreement, so no paired intervals are reported for it. Weights are not distributed; their hashes remain in [provenance.json](experiments/20260909-drafter-adaptation/evidence/provenance.json).

### Reusable Training Practices

1. **Measure the released draft model first.** All three English seeds reduced loss without improving paired agreement. Only seed 20260908 was serving-tested, with no observed throughput gain. Lower loss alone does not justify replacement.
2. **Train against the target that will serve.** Generate responses and extract hidden features, embeddings and output-head predictions from the same adapted target, not a mixture of base and fine-tuned targets.
3. **Record both losses.** Preserve `history`, `selector_history`, `gamma` and `selector_weight`. Falling backbone loss does not prove useful selector ranking.
4. **Save, reload, then evaluate.** Retain the checkpoint reload check, same-text held-out comparisons and all three vLLM routes.
5. **Keep failures in the evidence.** Screen failures, the opposing HF result, short-text exclusions and ungraded outcomes remain visible. Backbone and selector were trained together, so the experiment does not isolate the selector's contribution to gains.

## Architecture and Test Setup

The client and inference service share one host and communicate over loopback. Only one server mode runs at a time: baseline, MTP or DFlash. Switching modes keeps the client API unchanged. Client-side timing and grading of complete answers are separate from model inference.

![Test flow: client, inference service, draft model, records, grading and summaries](experiments/20260906-qwen38/images/test-flow-en.png)

*Original test-flow diagram based on the executed [runner](experiments/20260906-qwen38/source/campaign_runner.py), [stream timing](experiments/20260906-qwen38/source/stream_metrics.py) and [grader integration](experiments/20260906-qwen38/source/scoring.py); source in [test-flow-en.mmd](experiments/20260906-qwen38/images/test-flow-en.mmd). It separates inference, client measurements and grading; the three server modes do not run simultaneously.*

Draft Model adaptation follows a separate training path. Its stages also run on one GPU, without keeping the inference server resident during training:

![Draft Model-adaptation data and model flow](experiments/20260909-drafter-adaptation/images/training-flow-en.png)

*Original training-flow diagram based on [target training](experiments/20260909-drafter-adaptation/source/round4/finetune_target.py), [corpus generation](experiments/20260909-drafter-adaptation/source/round4/generate_responses.py), [draft training](experiments/20260909-drafter-adaptation/source/round4/train_drafter.py) and [paired measurement](experiments/20260909-drafter-adaptation/source/round4/analyze_predictability.py). The same figure generator renders the [diagram source](experiments/20260909-drafter-adaptation/images/training-flow.json). Training, save/reload and outcome evaluation are separate checks; the diagram is not runtime proof.*

## Quick Start

### Inference Serving and Requests

#### 1. Distinguish Target And Draft Weights

The roughly 3.8 GB file is the **DFlash 2 draft model, not the complete Qwen3.8-27B target or its MTP weights**. Do not pass it as `--model` with `method=mtp`. All three routes load the complete target checkpoint:

| Route | Target | Speculative configuration |
|---|---|---|
| Baseline | Qwen3.8-27B | Omit `--speculative-config` |
| MTP7 | Same target checkpoint, using its native MTP weights | `method="mtp"`, without a separate draft model |
| DFlash 2-7 | Same target plus the matched DFlash 2 draft | `method="dflash"`, with `model` pointing to the draft directory |

The archive records 18 target `.safetensors` files totaling 55,563,006,776 bytes (about 55.56 GB), and one draft weight file of 3,848,817,896 bytes (about 3.85 GB / 3.58 GiB). These are disk weight sizes, not total inference VRAM. **DFlash 2 is the checkpoint name; this vLLM version still uses `dflash`, not `dflash2` or `draft_model`, as the method.**

The commands use Linux x86_64, Bash and Python 3.12. The measured hardware was one H100 NVL, with a CUDA-13-compatible NVIDIA driver and enough VRAM for target, draft, KV cache and workspace. Capacity and numerical behavior on other GPUs need separate validation.

#### 2. Prepare Pinned Versions

Run every command below from `Deep-Learning/Speculative-Decoding` in this repository. Create the environment only for a first installation; activate an existing verified environment with the same versions instead of rebuilding it. Downloads require tens of GB of disk space.

```bash
python3 -m venv "$HOME/.venvs/qwen38-specdec"
source "$HOME/.venvs/qwen38-specdec/bin/activate"
python -m pip install 'vllm==0.28.0' 'torch==2.13.0' 'transformers==5.17.0'
python -m pip check

export MODEL_ROOT="$HOME/models/qwen38"
hf download Qwen/Qwen3.8-27B \
	--revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 \
	--local-dir "$MODEL_ROOT/target"
hf download incoai/Qwen3.8-27B-DFlash2 \
	--revision dedf8df68adfb1afeaf7b7480c0a0243108177b4 \
	--local-dir "$MODEL_ROOT/draft"
```

The directories must contain configuration, all weight shards and the target tokenizer, not a single shard alone. Key package versions come from the recorded installation; future resolution of all transitive dependencies is not guaranteed to reproduce identical environment bytes.

#### 3. Set Shared Server Parameters

Run once in the server terminal. All routes use this Bash array. Keep `dflash` out of the target's local path to avoid confusing path-based model identification with its actual role. Each launch writes logs to a separate directory under `$HOME/specdec-runs/`, preserving previous results.

```bash
set -euo pipefail
export VLLM_USE_V2_MODEL_RUNNER=1
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export RUN_DIR="$HOME/specdec-runs/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$RUN_DIR"

COMMON=(
	--model "$MODEL_ROOT/target"
	--served-model-name Qwen/Qwen3.8-27B
	--dtype bfloat16 --tensor-parallel-size 1
	--max-model-len 32768
	--max-num-seqs 16 --max-num-batched-tokens 16384
	--gpu-memory-utilization 0.9
	--no-enable-prefix-caching --kv-cache-dtype auto
	--attention-backend FLASH_ATTN --mamba-ssm-cache-dtype float32
	--reasoning-parser qwen3 --stream-interval 1
	--generation-config vllm --seed 20260906
	--limit-mm-per-prompt '{"image":0,"video":0,"audio":0}'
	--host 127.0.0.1 --port 18080
)
```

The target and KV cache use BF16, while the Mamba SSM cache is fixed to FP32. `max-num-seqs=16` is the server scheduler limit, not a requirement to send 16 concurrent client requests. `generation-config=vllm` prevents model-directory generation defaults from replacing explicit experiment settings.

#### 4. Start One Route

**Run only one route on this GPU and port at a time.** Finish a route, press `Ctrl+C` in its server terminal and use `nvidia-smi` to confirm that service has exited before starting the next one.

Baseline without speculation:

```bash
python -I -B -m vllm.entrypoints.openai.api_server "${COMMON[@]}" \
	2>&1 | tee "$RUN_DIR/baseline-server.log"
```

MTP7 using weights in the target checkpoint:

```bash
python -I -B -m vllm.entrypoints.openai.api_server "${COMMON[@]}" \
	--speculative-config '{"method":"mtp","num_speculative_tokens":7,"rejection_sample_method":"standard"}' \
	2>&1 | tee "$RUN_DIR/mtp7-server.log"
```

DFlash 2-7 with its additional draft checkpoint:

```bash
python -I -B -m vllm.entrypoints.openai.api_server "${COMMON[@]}" \
	--speculative-config "{\"method\":\"dflash\",\"model\":\"$MODEL_ROOT/draft\",\"num_speculative_tokens\":7,\"rejection_sample_method\":\"standard\"}" \
	2>&1 | tee "$RUN_DIR/dflash2_7-server.log"
```

In another terminal on the same host, check that `curl --fail http://127.0.0.1:18080/v1/models` returns `Qwen/Qwen3.8-27B`. Also inspect the startup log for the actual mode, V2 runner and precision; DFlash must load `DFlash2DraftModel`. Readiness proves loading only; send the real request below next.

#### 5. Configure Client Requests And Sampling

The client always calls the same `/v1/chat/completions` endpoint and `model` name. **MTP/DFlash selection is server-side, not a client switch.** There is no Web search or RAG in this experiment. Client settings mean sampling, thinking, output budget and request concurrency. `top_k=20` controls output sampling, not the server's seven draft tokens per cycle.

| Client setting | Recorded value |
|---|---|
| Sampling | `temperature=1.0`, `top_p=0.95`, `top_k=20`, `min_p=0.0` |
| Penalties | `presence_penalty=0.0`, `repetition_penalty=1.0` |
| Thinking | `reasoning_effort="xhigh"`; template enables and preserves thinking |
| Output limit | `max_completion_tokens=16384`, including thinking |
| Stream accounting | `stream=true`, `include_usage=true`, `return_token_ids=true`, `include_reasoning=true`, `stream_interval=1` |
| Client concurrency | The measured subset uses 1, 4 and 8; base seeds 20260906, 20260907 and 20260908 |

The actual per-task seed is `int(SHA256(f"{base_seed}|{task_id}")[:8], 16) % 2147483647`, not the base seed copied to every task. [Request examples](experiments/20260906-qwen38/evidence/request-examples.json) contain the full recorded JSON. Send one directly without reconstructing its prompt or parameters.

In the client terminal, enter the same `Deep-Learning/Speculative-Decoding` directory and activate the same Python environment, then run:

```bash
set -euo pipefail
source "$HOME/.venvs/qwen38-specdec/bin/activate"
CLIENT_RUN="$HOME/specdec-runs/client-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$CLIENT_RUN"
python -c 'import json; from pathlib import Path; samples=json.loads(Path("experiments/20260906-qwen38/evidence/request-examples.json").read_text(encoding="utf-8")); print(json.dumps(samples[0]["request"], ensure_ascii=False))' \
	> "$CLIENT_RUN/request.json"
curl --fail-with-body --no-buffer --connect-timeout 10 --max-time 600 \
	http://127.0.0.1:18080/v1/chat/completions \
	-H 'Content-Type: application/json' \
	--data-binary @"$CLIENT_RUN/request.json" \
	| tee "$CLIENT_RUN/response.sse"
```

`samples[0]` is the code task; use `samples[1]` for the math task. Inspect the complete SSE for generated `token_ids`, final `usage`, `finish_reason` and `[DONE]`. A `length` finish reason means the output limit was reached, not a normally completed answer. Use the same request JSON on all three routes. The curl timeout and recording here are for request reproduction only, not the performance measurement in the tables above.

#### Reproduction Scope

Commands are transcribed from the recorded installation, actual launch arguments and `server_command` in the [measurement source](experiments/20260906-qwen38/source/campaign_runner.py), with local paths replaced by environment variables. **Their scope is starting the three server modes and sending a request, not running the full performance and quality evaluation.** A new installation still needs model-loading and request checks; existing scores are not acceptance results for that environment.

**Verbatim replay on 2026-09-15.** The six code blocks above were executed once, as written and in order, in a clean `$HOME` (Python 3.12.3, H100 NVL 95 GB): `pip check` reported no conflicts after installation, and the installed versions were exactly vLLM 0.28.0, torch 2.13.0 and transformers 5.17.0; both weights were downloaded at the pinned revisions from the table above (32 + 5 files; the `config.json` SHA-256 values are recorded in the evidence directory); each of the three routes was launched once and `/v1/models` became ready after 135 s, 96 s and 105 s; one streamed request with `samples[0]` per route produced 772, 663 and 520 tokens, all with `finish_reason=stop` and a `[DONE]` marker. Per-block SHA-256 values, environment snapshots, `/v1/models` responses and the three complete SSE responses are in [readme-replay-20260915](experiments/20260906-qwen38/evidence/readme-replay-20260915/). The replay only shows that these commands complete one round in that environment; it produces no performance numbers, and the command blocks under "Reproducing the Adaptation" below were not replayed the same way.

Reproducing the score table additionally requires the same 64 tasks, 27 groups, frozen ordering, closed-loop concurrency, original measurement logic and EvalPlus/Math-Verify grading. The full preparation steps, task inputs and scheduling configuration required by `campaign_runner.py` are not yet packaged as a standalone public entry point, so the settings on this page cannot be passed directly to `--stage all`. Available files provide startup/request guidance and offline replay, not a standalone installer for the full 27-group experiment. Official method references: [MTP](https://github.com/vllm-project/vllm/blob/v0.28.0/docs/features/speculative_decoding/mtp.md), [pinned speculative configuration source](https://github.com/vllm-project/vllm/blob/2cf0a6915ce544dc493a0990f2ea38d81601128a/vllm/config/speculative.py).

### Reproducing the Adaptation

These steps reconstruct the executed scripts and arguments using a new user-owned run directory. Executed snapshots are in [source/round4/](experiments/20260909-drafter-adaptation/source/round4/). This documentation check did not rerun GPU training; a fresh environment still requires stage-by-stage validation.

**Environment:** Linux x86_64, Bash, Python 3.12 and a CUDA-13-compatible driver. The measured H100 NVL reported 95,830 MiB available, with about 86 GiB peak allocation for float32 draft master weights. **An 80 GB GPU is not validated for this recipe.** Disk must hold the base model, draft, trained checkpoints and another roughly 56 GB merged target, not just the downloads.

#### 1. Select the Setting and Prepare Environments

Start in this repository's `Deep-Learning/Speculative-Decoding` directory. `zh` selects Chinese all-module LoRA; `en` selects English attention-only LoRA. Use a new output directory per run and reuse the model-download directory.

```bash
set -euo pipefail
export ADAPT_LANGUAGE=zh
export ADAPT_EVIDENCE="$PWD/experiments/20260909-drafter-adaptation"
export ADAPT_SOURCE="$ADAPT_EVIDENCE/source/round4"
export ADAPT_RUN="$(mktemp -d "$HOME/drafter-adaptation.XXXXXXXX")"
export MODEL_ROOT="$HOME/models/qwen38"
export TRAIN_PYTHON="$HOME/.venvs/qwen38-drafter/bin/python"
export SERVE_PYTHON="$HOME/.venvs/qwen38-specdec/bin/python"
mkdir -p "$ADAPT_RUN"/{data,cache,out,served,results,logs}
printf 'export ADAPT_RUN=%q\nexport ADAPT_SOURCE=%q\n' "$ADAPT_RUN" "$ADAPT_SOURCE"
printf 'export TRAIN_PYTHON=%q\n' "$TRAIN_PYTHON"
```

Run this environment block only for a first installation. For existing environments with these versions, set the two Python paths and skip installation; do not rebuild environments per run.

```bash
python3.12 -m venv "$HOME/.venvs/qwen38-drafter"
"$TRAIN_PYTHON" -m pip install \
	torch==2.13.0 transformers==5.17.0 peft==0.20.0 \
	dflash==0.1.0 datasets huggingface_hub
python3.12 -m venv "$HOME/.venvs/qwen38-specdec"
"$SERVE_PYTHON" -m pip install \
	vllm==0.28.0 torch==2.13.0 transformers==5.17.0
"$TRAIN_PYTHON" -m pip check
"$SERVE_PYTHON" -m pip check
```

Both `pip check` commands must exit 0. These pins cover key packages, not all transitive dependencies.

#### 2. Obtain Pinned Weights and Data

```bash
"$(dirname "$TRAIN_PYTHON")/hf" download Qwen/Qwen3.8-27B \
	--revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 \
	--local-dir "$MODEL_ROOT/target"
"$(dirname "$TRAIN_PYTHON")/hf" download incoai/Qwen3.8-27B-DFlash2 \
	--revision dedf8df68adfb1afeaf7b7480c0a0243108177b4 \
	--local-dir "$MODEL_ROOT/draft"
"$TRAIN_PYTHON" "$ADAPT_SOURCE/prepare_domain_data.py" \
	--dataset FreedomIntelligence/medical-o1-reasoning-SFT --config "$ADAPT_LANGUAGE" \
	--question-field Question --response-field Response --instruction-field "" \
	--train-size 2000 --eval-size 200 --seed 20260909 --max-output-chars 1000000 \
	--out-dir "$ADAPT_RUN/data" 2>&1 | tee "$ADAPT_RUN/logs/prepare-data.log"
```

Outputs are 2,000 training questions, 200 held-out prompts and a split manifest. The dataset revision was not pinned at download time. Check train/eval hashes before training; matching row counts alone do not establish comparability.

```bash
"$TRAIN_PYTHON" - <<'PY'
import json, os
from pathlib import Path
run = Path(os.environ["ADAPT_RUN"])
evidence = Path(os.environ["ADAPT_EVIDENCE"])
language = os.environ["ADAPT_LANGUAGE"]
current = json.loads((run / "data/split_manifest.json").read_text())
recorded = json.loads((evidence / f"inputs/round4/split_manifest_{language}.json").read_text())
for field in ("train_sha256", "eval_sha256", "seed", "train_rows", "eval_rows"):
		assert current[field] == recorded[field], f"SPLIT_MISMATCH:{field}"
print("SPLIT_HASH_MATCH=PASS")
PY
```

#### 3. Fine-Tune the Target

The settings use different LoRA recipes. Data splitting uses seed `20260909`; target training uses `20260908`.

```bash
case "$ADAPT_LANGUAGE" in
	en) ADAPTER_ARGS=(--epochs 1 --lr 5e-5 --lora-rank 16 --lora-alpha 32
									 --target-modules attention) ;;
	zh) ADAPTER_ARGS=(--epochs 2 --lr 1e-4 --lora-rank 128 --lora-alpha 256
									 --target-modules all) ;;
	*) exit 2 ;;
esac
"$TRAIN_PYTHON" "$ADAPT_SOURCE/finetune_target.py" \
	--target "$MODEL_ROOT/target" --data "$ADAPT_RUN/data/train.jsonl" \
	--output "$ADAPT_RUN/out/adapter" --grad-accum 8 --max-length 1024 \
	--seed 20260908 "${ADAPTER_ARGS[@]}" \
	2>&1 | tee "$ADAPT_RUN/logs/target-training.log"
```

Check `FINETUNE_TARGET=PASS`, the adapter weights and `out/adapter/training_summary.json`. The summary preserves actual sample counts, dropped long samples, logged losses and memory. Split size is not necessarily training size.

#### 4. Screen Target Outputs and Record the Released Draft Model

The screen is a heuristic, not answer grading. The archived Chinese run returned `DEGENERATION_GATE=FAIL` here. Inspect failures before deciding whether to continue solely as a drafting study; do not silently ignore them or label quality as passing.

```bash
"$TRAIN_PYTHON" "$ADAPT_SOURCE/check_degeneration.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--prompts "$ADAPT_RUN/data/eval_prompts.jsonl" --limit 40 \
	--max-new-tokens 256 --repetition-unit token --label target \
	--output "$ADAPT_RUN/results/degeneration.json" \
	2>&1 | tee "$ADAPT_RUN/logs/target-screen.log"
```

After the screen passes, or after explicitly accepting an ungraded-answer research boundary, record the released draft model and cache the target responses that both draft models will share.

```bash
"$TRAIN_PYTHON" "$ADAPT_SOURCE/analyze_predictability.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--prompts "$ADAPT_RUN/data/eval_prompts.jsonl" --cache "$ADAPT_RUN/cache/target.pt" \
	--drafter "$MODEL_ROOT/draft" --draft-path selector --label released \
	--output "$ADAPT_RUN/results/pred-released.json" \
	2>&1 | tee "$ADAPT_RUN/logs/agreement-released.log"
```

#### 5. Generate Responses and Train the Draft Model and Selector

Questions come from the training split; the adapted target generates their responses. Do not replace these responses with dataset reference answers or include held-out prompts in draft training.

```bash
"$TRAIN_PYTHON" "$ADAPT_SOURCE/generate_responses.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--prompts "$ADAPT_RUN/data/train.jsonl" --output "$ADAPT_RUN/data/corpus.jsonl" \
	--limit 1200 --max-new-tokens 320 --batch-size 8 \
	2>&1 | tee "$ADAPT_RUN/logs/corpus.log"
```

Check `requested`, `written` and the hash in the adjacent `.manifest.json`. Empty responses can be skipped, so requested count is not automatically the usable corpus count.

```bash
"$TRAIN_PYTHON" "$ADAPT_SOURCE/train_drafter.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--drafter "$MODEL_ROOT/draft" --data "$ADAPT_RUN/data/corpus.jsonl" \
	--output "$ADAPT_RUN/out/drafter" --epochs 2 --limit 1200 \
	--anchors-per-sequence 8 --block 8 --max-length 1024 --gamma 7 \
	--lr 1e-4 --weight-decay 0.0 --warmup-fraction 0.05 --drafter-dtype float32 \
	--train-selector --selector-weight 1.0 --seed 20260908 \
	2>&1 | tee "$ADAPT_RUN/logs/drafter-training.log"
```

Check `TRAIN=PASS`; `checkpoint_reloads=true` records a save/reload weight-equality check. `out/drafter/training-history.json` stores `history` and `selector_history` separately. Lower loss does not replace held-out and serving checks. The other English training seeds are `1` and `2`; use separate output directories instead of overwriting this checkpoint.

#### 6. Evaluate Reloaded Draft Weights

Both draft models consume the same `target.pt`. Check cache and per-prompt text hashes before interpreting paired differences. Save HF end-to-end acceptance separately.

```bash
"$TRAIN_PYTHON" "$ADAPT_SOURCE/analyze_predictability.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--prompts "$ADAPT_RUN/data/eval_prompts.jsonl" --cache "$ADAPT_RUN/cache/target.pt" \
	--drafter "$ADAPT_RUN/out/drafter" --draft-path selector --label ours \
	--output "$ADAPT_RUN/results/pred-ours.json" \
	2>&1 | tee "$ADAPT_RUN/logs/agreement-ours.log"
"$TRAIN_PYTHON" "$ADAPT_SOURCE/measure_acceptance.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--prompts "$ADAPT_RUN/data/eval_prompts.jsonl" --limit 40 --max-new-tokens 256 \
	--drafter "$MODEL_ROOT/draft" --output "$ADAPT_RUN/results/acc-released.json" \
	--label released 2>&1 | tee "$ADAPT_RUN/logs/acceptance-released.log"
"$TRAIN_PYTHON" "$ADAPT_SOURCE/measure_acceptance.py" \
	--target "$MODEL_ROOT/target" --adapter "$ADAPT_RUN/out/adapter" \
	--prompts "$ADAPT_RUN/data/eval_prompts.jsonl" --limit 40 --max-new-tokens 256 \
	--drafter "$ADAPT_RUN/out/drafter" --output "$ADAPT_RUN/results/acc-ours.json" \
	--label ours 2>&1 | tee "$ADAPT_RUN/logs/acceptance-ours.log"
```

#### 7. Export Serving Weights Without Overwriting the Base

The draft exporter converts the weight-key layout. Save the merged target in a new directory; keep the downloaded base unchanged.

```bash
"$TRAIN_PYTHON" "$ADAPT_SOURCE/export_drafter_for_vllm.py" \
	--source "$ADAPT_RUN/out/drafter" --reference "$MODEL_ROOT/draft" \
	--output "$ADAPT_RUN/served/draft" 2>&1 | tee "$ADAPT_RUN/logs/export-draft.log"
"$TRAIN_PYTHON" - <<'PY'
import os, torch
from pathlib import Path
from transformers import AutoModelForCausalLM, AutoTokenizer
from peft import PeftModel
run = Path(os.environ["ADAPT_RUN"])
base = Path(os.environ["MODEL_ROOT"]) / "target"
tokenizer = AutoTokenizer.from_pretrained(base)
model = AutoModelForCausalLM.from_pretrained(base, dtype=torch.bfloat16, device_map="cuda")
model = PeftModel.from_pretrained(model, run / "out/adapter").merge_and_unload()
model.save_pretrained(run / "served/target", safe_serialization=True, max_shard_size="5GB")
tokenizer.save_pretrained(run / "served/target")
print("MERGED_TARGET_SAVED")
PY
```

Check `EXPORT_DRAFTER=PASS` and the merged-target directory. Subsequent steps still need to verify actual loading and requests.

#### 8. Server Terminal: Start One Route at a Time

After training exits, set the parameters shared by all three routes. Keep the server in the foreground; **do not append the client command after the blocking server command**.

```bash
export VLLM_USE_V2_MODEL_RUNNER=1 HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
ADAPT_SERVER_ARGS=(
	--model "$ADAPT_RUN/served/target" --served-model-name Qwen/Qwen3.8-27B
	--dtype bfloat16 --tensor-parallel-size 1 --max-model-len 8192
	--max-num-seqs 16 --max-num-batched-tokens 16384 --gpu-memory-utilization 0.9
	--no-enable-prefix-caching --kv-cache-dtype auto --attention-backend FLASH_ATTN
	--mamba-ssm-cache-dtype float32 --reasoning-parser qwen3 --generation-config vllm
	--seed 20260909 --limit-mm-per-prompt '{"image":0,"video":0,"audio":0}'
	--host 127.0.0.1 --port 18080
)
```

Baseline: the same fine-tuned target without a draft model.

```bash
"$SERVE_PYTHON" -m vllm.entrypoints.openai.api_server "${ADAPT_SERVER_ARGS[@]}" \
	2>&1 | tee "$ADAPT_RUN/logs/server-baseline.log"
```

Released draft model: finish baseline client measurements and stop its server before running this block.

```bash
"$SERVE_PYTHON" -m vllm.entrypoints.openai.api_server "${ADAPT_SERVER_ARGS[@]}" \
	--speculative-config "{\"method\":\"dflash\",\"model\":\"$MODEL_ROOT/draft\",
		\"num_speculative_tokens\":7,\"rejection_sample_method\":\"standard\"}" \
	2>&1 | tee "$ADAPT_RUN/logs/server-released.log"
```

Adapted draft model: stop the released-draft model server before running this block.

```bash
"$SERVE_PYTHON" -m vllm.entrypoints.openai.api_server "${ADAPT_SERVER_ARGS[@]}" \
	--speculative-config "{\"method\":\"dflash\",\"model\":\"$ADAPT_RUN/served/draft\",
		\"num_speculative_tokens\":7,\"rejection_sample_method\":\"standard\"}" \
	2>&1 | tee "$ADAPT_RUN/logs/server-ours.log"
```

#### 9. Client Terminal: Check the Service and Measure

Open another Bash terminal on the same host and execute the three `export` statements printed in step 1. They identify this exact run. Run the client once per active server, selecting `baseline`, `released` and `ours` for `ROUTE`; the files remain separate.

```bash
set -euo pipefail
export ROUTE=baseline
curl --fail http://127.0.0.1:18080/v1/models
"$TRAIN_PYTHON" "$ADAPT_SOURCE/vllm_client_bench.py" \
	--prompts "$ADAPT_RUN/data/eval_prompts.jsonl" --limit 40 --max-tokens 256 \
	--concurrency 1 4 --warmup 2 --label "$ROUTE" \
	--output "$ADAPT_RUN/results/vllm-$ROUTE.json" \
	2>&1 | tee "$ADAPT_RUN/logs/client-$ROUTE.log"
```

The model list must include `Qwen/Qwen3.8-27B`; draft-serving logs must show `DFlash2DraftModel`. The client must finish both concurrency levels and print `VLLM_CLIENT_BENCH=PASS`. Compare token counts, group duration, output limits and sampling in the three JSON files. Check server accepted/drafted counters separately from teacher-forced measurements.

#### 10. Stop Services and Retain Run Evidence

After each route, press `Ctrl+C` in the server terminal and confirm its process exited with `nvidia-smi` before starting the next. Retain `logs/`, `results/`, split and corpus manifests, both checkpoints and training histories. On a cloud host, also stop the billable resource when finished.

The repository's offline checks validate published evidence, not this new run. A hardware, dependency or data change requires new evaluation records; a paired bootstrap does not guarantee unchanged results across environments.

## Tests and Offline Replay

Every check below is offline: it reads saved records and this document. None of them starts a server, sends a request or regrades an answer.

| Check | Command | Accepted when |
|---|---|---|
| Report and evidence consistency | `validate_report.py` | Prints `REPORT_GATE=PASS` with one `RULE ... PASS` line per rule and exits 0 |
| Drift and refusal tests | `unittest discover` | All tests pass; each injected defect (changed table value, altered request, edited image, stale source, forged validation record, missing badge, collapsed section, nested Markdown) is rejected with its own error |
| Independent reaggregation | `analyze_results.py --groups` | The regenerated `summary.json` equals the published one |
| Previous experiment replay | `analyze_results.py --root ... --matrix` | All 3,100 requests, the frozen task set and grade bindings resolve |
| Adaptation summary and table | `experiments/20260909-drafter-adaptation/validate_report.py` | Recomputes every adaptation number from per-request records, requires the paired bootstrap to run only on byte-identical target text, scans for private identifiers and prints `ADAPTATION_GATE=PASS` |

Prerequisites: Python 3.10+ and its standard library, run from `Deep-Learning/Speculative-Decoding`. The previous-experiment replay requires Python 3.12. No GPU, network, credentials or extra packages are needed. The dedicated CI runs the first two checks on Windows and Linux with Python 3.10 and 3.12, and the replay on Python 3.12.

Not covered by these tests: fresh inference, official regrading, GPU-kernel behavior, and the figure generator, which needs Matplotlib and a CJK font and is therefore run manually.

```bash
python experiments/20260906-qwen38/validate_report.py
python -m unittest discover -s experiments/20260906-qwen38 -p "test_*.py"
python experiments/20260909-drafter-adaptation/validate_report.py
python -m unittest discover -s experiments/20260909-drafter-adaptation -p "test_*.py"
```

Validation should print `REPORT_GATE=PASS` and `ADAPTATION_GATE=PASS`, all tests should pass, and every command should exit with code 0. They check that this document's tables, saved scores and file hashes agree.

To independently check summary values from the per-group records:

```bash
python experiments/20260906-qwen38/analyze_results.py --groups experiments/20260906-qwen38/data/groups.json --output experiments/20260906-qwen38/regenerated
```

Compare `summary.json` in the output directory with the [published summary](experiments/20260906-qwen38/data/summary.json). The program only reads saved grades, counts and timing; it does not execute generated answers.

Replaying the previous experiment requires Python 3.12. It checks all 3,100 requests, the frozen task set, task/repeat counts and grade binding; missing or mismatched items fail instead of shrinking the denominator:

```bash
python experiments/20260905-quality/src/analyze_results.py --root experiments/20260905-quality --output out/20260905-replayed.json --matrix
```

## Compatibility and Limits

**Does modifying the target require retraining its draft model? Not automatically.** The published checkpoint used here is `incoai/Qwen3.8-27B-DFlash2`, intended for `Qwen/Qwen3.8-27B`. Reuse with another target needs compatible architecture, tokenizer, hidden features and output head, followed by workload-specific evaluation. A shared family name does not establish compatibility; fine-tuning alone does not establish incompatibility.

Native MTP likewise needs compatible model architecture, MTP weights and engine support. Enabling a serving flag is not a method for creating missing MTP weights.

**DFlash: Block Diffusion for Flash Speculative Decoding** uses a small block-diffusion model to draft several tokens in parallel, conditioned on the target's hidden features. For adaptation, the teacher is the exact target that will serve requests, including any adapter. The starting student is an existing draft checkpoint. Prompts come from the intended workload; the teacher generates responses, and its parameters remain frozen while the draft parameters are updated. This is continuation training, not training a draft model from scratch.

The following describes the [DFlash paper, Sections 4.2 and A.1](https://arxiv.org/html/2602.06036v2#S4.SS2), not a reproduction of the complete DFlash 2 training recipe.

- **Training responses:** The paper uses about 800K samples from Nemotron Post-Training V2 and CodeAlpaca, with target-generated responses. That is its experimental scale, not a demonstrated minimum for every adaptation.
- **Conditioning:** Hidden states from five target layers, sampled between the second and the third-to-last layer, are concatenated, projected once, and injected into the key and value entries of every draft layer.
- **Block construction:** Anchor tokens are sampled at random from the response; the remaining positions of each block are masked and predicted in parallel.
- **Loss:** Cross-entropy is weighted by `exp(-(k-1)/gamma)` over the position `k` inside a block, because an error early in a block invalidates every later position.
- **Shared parameters:** The target, its token embedding and its language-model head remain frozen; training updates the draft model rather than the target.

Fine-tuning can change the target's features and token predictions. Which parameters change depends on the tuning recipe; an adapter does not necessarily update the embedding or output-head weights. These dependencies motivate a comparison, not a conclusion that the released draft model must fail or that adaptation must improve it.

**Measure three different outcomes.** Draft-token agreement measures how well the draft predicts the target; answer quality measures whether the final response solves the task; throughput and latency measure the actual service. A lower training loss or higher draft agreement cannot substitute for graded answers and faster serving. Speculative decoding's theoretical output-distribution guarantee assumes a correct verification and sampling implementation. It is not evidence that a particular engine, precision or cache path preserves answers.

The paper provides one adaptation example: [Section 5.4, Table 4](https://arxiv.org/html/2602.06036v2#S5.SS4) adapts a Qwen3.5-27B DFlash draft model using 1.6K LongAlign-10K samples for three epochs. On HotpotQA at 16K context, acceptance length changes from 3.61 to 6.05. This is the authors' long-context result, not this repository's Qwen3.8-27B fine-tuned-target result or a general time/cost guarantee.

| Situation | What to establish before adoption |
|---|---|
| Target with an intended released draft model | Compare no speculation and the released draft model under the same workload, concurrency and output-quality criteria |
| Fine-tuned target with a compatible released draft model | Test the released draft model first. If adapting it, compare both draft checkpoints against the same frozen target; save and reload the adapted checkpoint before evaluation |
| No compatible draft checkpoint | Treat training a new draft model as a separate project. An existing-checkpoint adaptation result does not demonstrate from-scratch training capability |

The repository keeps inference-comparison and draft model-adaptation evidence separately. Continuation training was executed and evaluated for the recorded target and data settings, not validated as a universal recipe. Offline checks verify saved records; they do not certify training or answer quality in a fresh environment.

### How MTP and DFlash Differ

Speculative decoding uses a smaller draft model to propose candidates, then asks the target model to verify them. The target still controls which tokens enter the output.

| Aspect | MTP7 in this run | DFlash 2-7 in this run |
|---|---|---|
| Draft weights | MTP weights shipped in the Qwen3.8 checkpoint | A DFlash 2 checkpoint trained for the target |
| Candidate production | Sequential draft steps in the tested vLLM path | Block diffusion to draft a block in parallel |
| Candidates per cycle | 7 tokens | 7 tokens |
| Verification | The same Qwen3.8 target model | The same Qwen3.8 target model |

Seven is a candidate count, not network depth. One forward pass still traverses the draft model's layers. Whether MTP weights are published separately depends on the model; this run's packaging is not a universal definition of MTP.

The benefit depends on drafting and verification time per cycle, and how many tokens that cycle actually advances. More candidates need not be faster; acceptance rate is not answer accuracy. Algorithmic distribution guarantees also require a correct engine implementation and do not replace deployment quality tests.

See the [DFlash paper](https://arxiv.org/abs/2602.06036) and [pinned vLLM source](https://github.com/vllm-project/vllm/tree/2cf0a6915ce544dc493a0990f2ea38d81601128a) for mechanism context, and the [recorded configuration](experiments/20260906-qwen38/evidence/configuration.json) for this run's settings.

### Scope of the Conclusion

- Results describe this fixed configuration and subset; they do not prove statistical significance, distribution equivalence or formal noninferiority.
- Throughput, client latency and correct-answer delivery measure different things. They are not interchangeable and do not establish isolated GPU-kernel performance.
- The model, checkpoint and engine changed. This run does not establish that the previous DFlash concurrency failure was fixed. The experiments remain separate.

### What Training From Scratch Would Take

This repository has **not** trained a DFlash draft model from scratch. Everything above starts from the released checkpoint. The only random-initialization run was a CPU canary on a toy configuration (hidden size 128, vocabulary 512), using [`stage0_gradient_canary.py`](experiments/20260909-drafter-adaptation/source/round4/stage0_gradient_canary.py): 30 steps, loss 6.24 → 3.88 in the author's single run, whose log was not archived. It shows that gradients reach the draft layers, the fused target-feature projection and the norms while the frozen target receives none. It does not show that a real-size draft model converges, and it is not evidence of from-scratch capability.

The paper's recipe ([Section 5 and Appendix A.1](https://arxiv.org/html/2602.06036v2#A1.SS1)): about 800K prompts from Nemotron Post-Training V2 and CodeAlpaca with responses regenerated by the target; 6 epochs, AdamW at 6e-4, cosine schedule with 4% warmup, sequences up to 3,072 tokens, and 512 anchor positions per sequence trained jointly through one sparse attention mask. The paper's ablations use 100K samples and reach roughly three quarters of the full-data speedup (Qwen3-4B on MATH-500: 4.71× versus 6.09×). The paper names H200 GPUs but not the GPU count or training hours.

What the code in this repository lacks for that recipe, in order of cost:

1. `dflash` 0.1.0 constructs `GroupedDynamicCausalConv.base_kernel` with `torch.empty`. Built from a config instead of a checkpoint, the first forward pass is NaN. The canary initializes it as an identity tap; that fix has not been exercised at real size.
2. `train_drafter.py` processes 8 anchors per sequence, one block per forward pass. The paper's 512 anchors through one sparse-attention pass is about 64× more draft supervision per target forward. On this loop, paper-scale training would cost thousands of GPU-hours.
3. The DFlash 2 candidate-selector objective is the author's construction and has only run from released weights. A from-scratch pilot should target the paper's DFlash architecture without the selector, for which z-lab publishes reference checkpoints to compare against.
4. `generate_responses.py` uses Hugging Face `generate` at batch 8, about 70 output tokens per second on the 27B target. Paper-scale data needs a serving engine.
5. Single GPU only; no data-parallel training.

Order-of-magnitude estimates, extrapolated from the measured 0.61 s per training step and the vLLM throughput above, and assuming items 1–5 are done first:

| Run | Target | Data | GPUs | Time |
|---|---|---|---|---|
| Smallest defensible from-scratch pilot | Qwen3-8B, compared against z-lab's public DFlash checkpoint | 100K prompts, self-generated responses, 6 epochs | 1–2 H100-class | About half a day to one day of generation, then two to four days of training on one GPU |
| Paper scale | Qwen3-8B | 800K prompts, 6 epochs | About 8 | Three to four days |
| Paper scale | Qwen3.8-27B | 800K prompts, 6 epochs | At least 8, each with more than 94 GiB (the paper used H200) | About 1.5 days of generation on 4 GPUs, then about a week of training; one 94 GiB GPU already peaked at 86 GiB at sequence length 1,024 |

These are estimates, not measurements. The defensible statement today: the adaptation path is measured; the training objective is implemented and shown to raise paired draft agreement; from-scratch training at real size has not been demonstrated.

### Previous Experiment: Qwen3.6-27B and First-Generation DFlash (2026-09-05)

<a id="previous-experiment"></a>

The previous run used Qwen3.6-27B, the first-generation DFlash draft model and vLLM 0.21.0 on the same H100 NVL, and completed all 164 HumanEval+ and 500 MATH-500 tasks with complete answers. **DFlash15 was faster per request and its primary scores were close to the baseline, but at concurrency 4 and 8 answer quality on the same 32 code and 32 math tasks regressed substantially.** The cause has not been identified, and no fixed configuration has been retested.

The two runs differ in target model, draft model, engine, task scope and sampling. Not reproducing the old failure in the newer combination does not establish that the old defect was fixed.

#### Primary Evaluation: Full Datasets, Once per Task

Code had to pass both the base and extended official EvalPlus tests; math was graded by the pinned official Math-Verify script. Length-stopped responses stay in the denominator.

| Route | HumanEval+ | MATH-500 | Code base tests | Math length stops |
|---|---:|---:|---:|---:|
| Baseline | 152/164 (92.68%) | 489/500 (97.80%) | 159/164 | 4 |
| MTP5 | 155/164 (94.51%) | 494/500 (98.80%) | 162/164 | 2 |
| DFlash15 | 153/164 (93.29%) | 490/500 (98.00%) | 160/164 | 3 |

Median code request times for Baseline, MTP5 and DFlash15 were 4.393, 1.175 and 0.660 seconds; math 15.666, 4.542 and 2.980 seconds. Median code output rates were 53.61, 196.01 and 367.52 tok/s; math 54.05, 187.60 and 281.77 tok/s. Timing includes prefill and same-host client overhead and excludes server startup. Computing MTP5 time divided by DFlash15 time per task and then taking the median gives 1.861 for code and 1.498 for math. Five versus fifteen draft tokens is not an equal compute budget, nor a tuned best configuration per route.

![Previous run: complete-answer request time](experiments/20260905-quality/analysis/figures/primary-latency.png)

*Author's measurements, run dflash-quality-20260905, 164 code and 500 math tasks per route, once each. Values come from the [per-task replay summary](experiments/20260905-quality/analysis/summary.json). Total request time over all answers, not isolated decode-kernel time.*

#### Concurrency Quality Did Not Pass

Each level used the first 32 code and first 32 math tasks of the frozen manifest, once each. Concurrency is the number of in-flight requests from the same-host client, not an arrival rate.

| Route | Concurrency | HumanEval+ | Math subset | Length stops (code/math) |
|---|---:|---:|---:|---:|
| Baseline | 1 | 32/32 | 32/32 | 0/0 |
| Baseline | 4 | 32/32 | 30/32 | 0/0 |
| Baseline | 8 | 32/32 | 31/32 | 0/0 |
| MTP5 | 1 | 32/32 | 31/32 | 0/0 |
| MTP5 | 4 | 32/32 | 31/32 | 0/0 |
| MTP5 | 8 | 32/32 | 32/32 | 0/0 |
| DFlash15 | 1 | 32/32 | 31/32 | 0/1 |
| DFlash15 | 4 | 11/32 | 13/32 | 8/17 |
| DFlash15 | 8 | 10/32 | 12/32 | 13/20 |

![Previous run: correct answers under concurrency on the same tasks](experiments/20260905-quality/analysis/figures/concurrency-quality.png)

*Author's measurements, the same 32+32 tasks, once per level. Raw responses and official grades are in the [results directory](experiments/20260905-quality/results/); the summary is in the [analysis output](experiments/20260905-quality/analysis/summary.json). The anomaly is bound to that tested combination; the curves are not a root-cause proof.*

HumanEval/2 is a concrete example: the normalized request hash is identical at all three levels. [Concurrency 1](experiments/20260905-quality/results/dflash15/concurrency-1/repeat-0/HumanEval_2.json) returned a correct function; [concurrency 4](experiments/20260905-quality/results/dflash15/concurrency-4/repeat-0/HumanEval_2.json) returned an empty definition and a JSON fragment; [concurrency 8](experiments/20260905-quality/results/dflash15/concurrency-8/repeat-0/HumanEval_2.json) produced unrelated function names and repeated text until the 4,096-token limit. That DFlash15 configuration cannot carry concurrent traffic on the strength of single-request results, and the evidence does not attribute the cause to a vLLM component, floating-point error, DFlash theory, the H100 or the cloud platform.

Each primary route has 1,012 responses (664 primary, 48 same-seed repeats, 48 streaming, 192 concurrency, 48 random sampling, 12 synthetic retrieval); with 64 matched-window DFlash5 responses, the total is **3,100 responses across 25 route/scenario combinations**. DFlash5 scored 32/32 on both code and math; its concurrency was not tested.

#### Previous Run: Fixed Method

| Item | Recorded value |
|---|---|
| GPU | One NVIDIA H100 NVL, 95,830 MiB; driver 610.57.04 |
| Target | `Qwen/Qwen3.6-27B` @ `6a9e13bd6fc8f0983b9b99948120bc37f49c13e9`, BF16 |
| Draft model | `z-lab/Qwen3.6-27B-DFlash` @ `0919688658996800f86b895034249700e9481106` |
| Generation environment | vLLM 0.21.0, PyTorch 2.11.0, transformers 4.57.6 |
| Graders | EvalPlus @ `26d6d00bb1fd0fa37f39c99d5290da67891d1c5e`; Math-Verify @ `ba3d3aaff23b3f4cac7a14672b4f6e293d97c98b` |
| Datasets | HumanEval+ v0.1.10; MATH-500 @ `6e4ed1a2a79af7d8630a6b768ec859cb5af4d3be` |
| Primary sampling | temperature 0, top_p 1, top_k -1, seed 20260905; `enable_thinking=false` |
| Server settings | max_model_len 40960, max_num_seqs 16, max_num_batched_tokens 8192, GPU memory utilization 0.9, prefix caching disabled |
| Output budget | Code 4096, math 8192 |

File hashes for the model, configuration and tokenizer, plus package versions, are in [inputs.json](experiments/20260905-quality/metadata/inputs.json). The [public protocol](experiments/20260905-quality/src/experiment.json) only removes private resource-management objects; the [projection record](experiments/20260905-quality/metadata/protocol-public-projection.json) records hashes before and after. Code grading ran in an offline, unprivileged Docker container.

#### Rerunning the Previous Generation

Requires Linux, Python 3.12, a compatible H100 NVL environment and disk space for both weight snapshots and dependencies. The grading container runs as UID/GID 1000 and the host must allow `sudo -n` Docker calls. Start from `experiments/20260905-quality`, create a new run directory and do not overwrite the provided evidence.

```bash
set -euo pipefail
SOURCE="$PWD"
export DFLASH_RUN_ROOT="$(mktemp -d "$HOME/quality-replay-XXXXXXXX")"
export DFLASH_CACHE_ROOT="$DFLASH_RUN_ROOT/cache"
export DFLASH_TARGET_PATH="$DFLASH_CACHE_ROOT/target"
[[ "${DFLASH_TARGET_PATH,,}" != *dflash* ]]
mkdir -p "$DFLASH_RUN_ROOT"/{src,data,metadata,logs,results,state,upstream}
cp src/quality_runner.py src/prepare_data.py src/experiment.json "$DFLASH_RUN_ROOT/src/"
cp data/math500.jsonl data/humaneval_plus.jsonl "$DFLASH_RUN_ROOT/data/"
python3.12 -m venv "$DFLASH_CACHE_ROOT/venv"
PYTHON="$DFLASH_CACHE_ROOT/venv/bin/python"
"$PYTHON" -m pip install -r "$SOURCE/metadata/requirements-frozen.txt"
"$DFLASH_CACHE_ROOT/venv/bin/hf" download Qwen/Qwen3.6-27B --revision 6a9e13bd6fc8f0983b9b99948120bc37f49c13e9 --local-dir "$DFLASH_CACHE_ROOT/target"
"$DFLASH_CACHE_ROOT/venv/bin/hf" download z-lab/Qwen3.6-27B-DFlash --revision 0919688658996800f86b895034249700e9481106 --local-dir "$DFLASH_CACHE_ROOT/draft"
curl --fail --location 'https://raw.githubusercontent.com/huggingface/Math-Verify/ba3d3aaff23b3f4cac7a14672b4f6e293d97c98b/evaluate_model_outputs.py' -o "$DFLASH_RUN_ROOT/upstream/math-verify-evaluate.py"
curl --fail --location 'https://github.com/evalplus/mbppplus_release/releases/download/v0.2.0/MbppPlus.jsonl.gz' -o "$DFLASH_RUN_ROOT/upstream/mbpp-plus-v0.2.0.jsonl.gz"
gzip -dc "$DFLASH_RUN_ROOT/upstream/mbpp-plus-v0.2.0.jsonl.gz" > "$DFLASH_RUN_ROOT/upstream/mbpp-plus-v0.2.0.jsonl"
HUMANEVAL_OVERRIDE_PATH="$DFLASH_RUN_ROOT/data/humaneval_plus.jsonl" "$PYTHON" src/prepare_data.py --root "$DFLASH_RUN_ROOT" --cache "$DFLASH_CACHE_ROOT"
sudo -n docker build -f src/Dockerfile.eval -t dflash-quality-eval:20260905 .
"$PYTHON" "$DFLASH_RUN_ROOT/src/quality_runner.py" --phase canary
"$PYTHON" "$DFLASH_RUN_ROOT/src/quality_runner.py" --phase full
"$PYTHON" "$DFLASH_RUN_ROOT/src/quality_runner.py" --phase full --route dflash5
printf '{"phase":"COMPLETE","exit_code":0}\n' > "$DFLASH_RUN_ROOT/state/campaign.json"
"$PYTHON" "$SOURCE/src/analyze_results.py" --root "$DFLASH_RUN_ROOT" --output "$DFLASH_RUN_ROOT/analysis/summary.json" --matrix
```

The generation CLI stops only its own model server and does not release the host. Compare against the [grader dependency versions](experiments/20260905-quality/metadata/evaluator-requirements-frozen.txt) and the [original image ID](experiments/20260905-quality/metadata/evaluator-image-id.txt) when rerunning; even with pinned grader commits, Docker base tags and system packages can change. vLLM 0.21.0 accepts `standard` but not `strict`, and a target path containing `dflash` can trigger a wrong method inference.

## Tools and Evidence

| Path | Contents |
|---|---|
| [`experiments/20260906-qwen38/`](experiments/20260906-qwen38/) | The current run: group records, summary, evidence, executed source snapshots, analyzer, validator, tests and the test-flow diagram |
| [`experiments/20260909-drafter-adaptation/`](experiments/20260909-drafter-adaptation/) | The draft model-adaptation experiment: exported per-request results for both drift regimes, executed script snapshots, provenance hashes, analyzer, validator and tests |
| [`experiments/20260905-quality/`](experiments/20260905-quality/) | The previous complete-answer run: raw responses, official grades, per-task comparisons, analysis code and figures |
| [`images/`](images/) | The Chinese result figures used by [README_CN.md](README_CN.md) |
| [`tools/make_readme_figures.py`](tools/make_readme_figures.py) | Regenerates those Chinese figures from both experiments' published summaries; needs a CJK font and [the pinned Matplotlib](experiments/20260906-qwen38/requirements-figures.txt) |
| [`LICENSE`](LICENSE) | License covering this directory |

### Evidence and Code

| Entry | What to verify |
|---|---|
| [Runner](experiments/20260906-qwen38/source/campaign_runner.py) | The dispatch, timing and campaign-control code used in the run |
| [Grader integration](experiments/20260906-qwen38/source/scoring.py), [stream timing](experiments/20260906-qwen38/source/stream_metrics.py) | Response/grade binding, token accounting and latency calculation |
| [Configuration](experiments/20260906-qwen38/evidence/configuration.json), [requests](experiments/20260906-qwen38/evidence/request-examples.json) | Fixed settings and two hashed actual payloads |
| [Experiment record](experiments/20260906-qwen38/evidence/run.json) | Coverage, activation checks, measured durations and source-member hashes |
| [Groups](experiments/20260906-qwen38/data/groups.json), [summary](experiments/20260906-qwen38/data/summary.json) | Task IDs, saved scores, timing, counters and matched comparisons |
| [Analyzer](experiments/20260906-qwen38/analyze_results.py), [validator](experiments/20260906-qwen38/validate_report.py), [tests](experiments/20260906-qwen38/test_report.py) | Reaggregation and checks that this document's tables, links, badges and evidence agree |
| [Previous analysis](experiments/20260905-quality/analysis/), [previous results](experiments/20260905-quality/results/), [previous source](experiments/20260905-quality/src/) | 2026-09-05 per-task comparisons, raw responses, official grades and analysis code |
| [Adaptation results](experiments/20260909-drafter-adaptation/results/), [summary](experiments/20260909-drafter-adaptation/data/summary.json), [provenance](experiments/20260909-drafter-adaptation/evidence/provenance.json), [scripts](experiments/20260909-drafter-adaptation/source/) | Per-request draft model agreement, acceptance and vLLM records for both regimes; weight, data and log hashes; the training and measurement scripts as executed |

These are snapshots of the executed source, not a complete fresh-GPU installation bundle. **Complete raw answers and SSE streams remain privately archived by the author and are not redistributed here.** The public files exclude infrastructure locators and credentials. Archive and member hashes describe provenance, not independent proof of runtime behavior.

### Official Sources

- [Classical speculative decoding](https://proceedings.mlr.press/v202/leviathan23a.html)
- [DFlash paper](https://arxiv.org/abs/2602.06036) and [project source](https://github.com/z-lab/dflash)
- [vLLM 0.28.0](https://github.com/vllm-project/vllm/releases/tag/v0.28.0)
- [EvalPlus](https://github.com/evalplus/evalplus) and [MATH-500 provenance](https://github.com/openai/prm800k#math-splits)
