# Lenovo Qira — Follow-up: API-path latency, stepped concurrency, judge recalibration

Three paid follow-up runs answer the questions the first two studies left open: whether the whole-answer bursts seen on the Chat Completions direct baselines were an API-path artefact, how the three candidate models and the balanced router behave at 4/8/16 concurrent requests, and how much the blind judge's scores move once the rubric states explicitly that the assistant has no tools. Same Sweden Central VM and resource, no search or tools, hardened harness (no silent SDK retries, usage required).

> Author: **Xinyu Wei (魏新宇)** · 2026-09-10 · runs `direct_20260910_074346`, `loadtest_20260910_085128`, `quality_v2_*`

[English](README.md) | [中文](README-CN.md)

Related: [scenario benchmark](../qira-scenario-model-benchmark/README.md) · [router validation](../qira-model-router-validation/README.md)

## 1. Same deployments, two API paths: on Chat the first token arrived with the last (497/564); on Responses it did not (32/564)

`gpt-5.6-sol-dz` and `gpt-5.6-luna-dz` (DataZoneStandard) were re-run on the **Responses API** with the identical 47 prompts, efforts and 1 + 3 iterations. On the Chat path 497/564 measured answers arrived in one burst (<50 ms first-to-last delta); on the Responses path **32/564** did. TTFT/E2E are client-observed; the two paths were measured on different days, so absolute latency also carries a time-of-run effect.

| Arm | API path | TTFT P50 / P90 s | E2E P50 / P90 s | TPOT P50 ms | tok/s P50 | Bursts <50 ms | Output tok | USD / 1,000 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| gpt-5.6-sol-dz | Chat | 2.08 / 7.68 | 2.09 / 11.58 | 0.09 | 10669 | 127/141 | 360 | 11.24 |
| gpt-5.6-sol-dz | Responses | 0.86 / 4.67 | 2.67 / 13.32 | 14.44 | 69 | 10/141 | 341 | 10.65 |
| gpt-5.6-sol-dz@low | Chat | 1.78 / 6.82 | 1.80 / 9.57 | 0.09 | 10759 | 125/141 | 312 | 9.78 |
| gpt-5.6-sol-dz@low | Responses | 0.99 / 3.66 | 2.82 / 13.18 | 15.12 | 66 | 7/141 | 302 | 9.48 |
| gpt-5.6-luna-dz | Chat | 1.57 / 5.29 | 1.57 / 8.67 | 0.09 | 10927 | 126/141 | 372 | 0.46 |
| gpt-5.6-luna-dz | Responses | 1.06 / 3.76 | 2.47 / 12.32 | 9.82 | 102 | 9/141 | 375 | 0.47 |
| gpt-5.6-luna-dz@low | Chat | 1.27 / 4.32 | 1.28 / 6.54 | 0.09 | 10770 | 119/141 | 343 | 0.43 |
| gpt-5.6-luna-dz@low | Responses | 0.67 / 3.03 | 2.08 / 8.62 | 10.51 | 95 | 6/141 | 321 | 0.40 |

- Direct Sol, effort not sent: TTFT P50 **2.08 s on Chat vs 0.86 s on Responses**; the Chat E2E P50 (2.09 s) was within 15 ms of its TTFT — the first token arrived together with the last one. Direct Luna: 1.57 s vs 1.06 s. Taking the Responses TTFT as the model-side component, the delivery share of the Chat TTFT ranges from 33% to 58% across the four arms — large, but not uniformly "most" of it, and confounded by the different measurement days.
- On the Responses path the per-token pace is measurable: Sol 14.4 ms/token (69 tok/s), Luna 9.8 ms/token (102 tok/s). The Chat-path `decode_tps` of ~10,000 in the router study was the burst, as that report already stated.
- **Consequence for the router study:** the router arms (Chat path, GlobalStandard) and the direct baselines (Chat path, DataZoneStandard) both carried this delivery behaviour to different degrees (router arms ≈ 20–25 % bursts, direct ≈ 90 %). Paired TTFT differences between them remain uninterpretable as router overhead; that report already refuses that reading. Whether the burst comes from the DataZone serving path or from the Chat Completions streaming layer is **still not isolated** — this run changed the API only.

## 2. Stepped concurrency: 48-request batches with ≤ 1 / 4 / 8 / 16 in flight

48 requests per level, cycling the 17 Qira prompts, submitted to a thread pool of <level> workers; one warm-up per arm excluded; levels run in ascending order with a 10 s settle between them; arms run one after another. Measurement code is `harness.run_one` unchanged. **This is a closed batch, not a steady-state load:** in-flight equals the level only until the queue drains, then decays to zero while the longest answers finish. The wall-clock (and therefore every aggregate below) includes that drain; the *share of wall at target* column, measured from the per-request start/finish offsets, says how much of each window actually ran at the stated concurrency. `gpt-4o-mini-bench`, `gpt-5-mini`, `gpt-5.6-luna` are GlobalStandard capacity 1000 on the Responses API; `router-sol-luna-balanced` is GlobalStandard capacity 300 on Chat Completions.

| Arm | In flight | Completed | 429 | Wall s | Share of wall at target | req/s | Agg out tok/s (incl. reasoning) | Agg visible tok/s | TTFT P50 / P90 s | E2E P50 / P90 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gpt-4o-mini-bench | ≤1 | 48/48 | 0 | 191.9 | 100% | 0.25 | 36 | 36 | 0.44 / 1.25 | 2.77 / 5.85 |
| gpt-4o-mini-bench | ≤4 | 48/48 | 0 | 49.4 | 81% | 0.97 | 130 | 130 | 0.48 / 1.25 | 2.72 / 5.11 |
| gpt-4o-mini-bench | ≤8 | 48/48 | 0 | 30.0 | 65% | 1.60 | 223 | 223 | 0.44 / 1.88 | 2.74 / 7.92 |
| gpt-4o-mini-bench | ≤16 | 48/48 | 0 | 18.1 | 45% | 2.65 | 355 | 355 | 0.53 / 2.21 | 2.65 / 5.51 |
| gpt-5-mini@minimal | ≤1 | 48/48 | 0 | 167.8 | 100% | 0.29 | 80 | 80 | 0.63 / 1.34 | 2.09 / 5.10 |
| gpt-5-mini@minimal | ≤4 | 48/48 | 0 | 51.0 | 82% | 0.94 | 277 | 277 | 0.69 / 1.50 | 2.65 / 7.42 |
| gpt-5-mini@minimal | ≤8 | 48/48 | 0 | 33.2 | 73% | 1.45 | 420 | 420 | 0.90 / 1.83 | 2.89 / 10.90 |
| gpt-5-mini@minimal | ≤16 | 48/48 | 0 | 21.4 | 34% | 2.24 | 620 | 620 | 0.77 / 1.58 | 2.38 / 5.16 |
| gpt-5.6-luna@none | ≤1 | 48/48 | 0 | 171.2 | 100% | 0.28 | 70 | 70 | 1.07 / 1.52 | 2.48 / 4.39 |
| gpt-5.6-luna@none | ≤4 | 48/48 | 0 | 60.2 | 75% | 0.80 | 214 | 214 | 0.89 / 1.74 | 2.16 / 6.91 |
| gpt-5.6-luna@none | ≤8 | 48/48 | 0 | 39.1 | 50% | 1.23 | 332 | 332 | 0.98 / 1.85 | 2.54 / 5.60 |
| gpt-5.6-luna@none | ≤16 | 48/48 | 0 | 33.1 | 22% | 1.45 | 386 | 386 | 0.91 / 1.43 | 2.55 / 5.72 |
| router-sol-luna-balanced | ≤1 | 48/48 | 0 | 196.6 | 100% | 0.24 | 75 | 65 | 1.74 / 3.51 | 2.92 / 5.83 |
| router-sol-luna-balanced | ≤4 | 48/48 | 0 | 49.5 | 85% | 0.97 | 291 | 253 | 1.81 / 3.48 | 2.72 / 5.15 |
| router-sol-luna-balanced | ≤8 | 48/48 | 0 | 31.7 | 56% | 1.51 | 465 | 404 | 1.71 / 3.11 | 2.47 / 4.99 |
| router-sol-luna-balanced | ≤16 | 48/48 | 0 | 23.6 | 31% | 2.04 | 604 | 522 | 1.97 / 3.52 | 2.63 / 5.06 |

- **0 requests returned 429 and 0 other errors across 768 measured requests.** With `max_retries=0`, every one would have been recorded. At capacity 1000 (≈1M TPM) and 16 concurrent short prompts, this test does not reach the PAYGO rate limit; the 429 onset is a function of the deployment's capacity setting, not of the model, and was not found here.
- **Do not read the ≤16 rows as "throughput at 16 in flight".** At ≤16 the target concurrency held for only 22%–45% of the wall (≤4: 75%–85%); the rest of each window is a few long WFM01 answers draining alone, so the aggregate tok/s at ≤16 is dominated by how long each model's longest answer is. A steady-state figure needs a fixed-duration window with continuous refill, or ≥10× level requests per level — neither was run. TTFT/E2E percentiles are far less affected: every request started at a moment when in-flight had just refilled to the level.
- `router-sol-luna-balanced` served **gpt-5.6-luna** for all 192 completed load-test requests (17 Qira prompts), consistent with the router study where balanced never selected Sol for a Qira prompt. Because no effort was sent, the router-served Luna ran with default reasoning (13%–14% of its output tokens are reasoning tokens) while the three `@none`/`@minimal`/non-reasoning arms had 0%: compare the router on the *visible* tok/s column, not the one including reasoning. Its per-request tok/s (in the CSV) also carries the Chat-path delivery effect (29/192 bursts); wall-clock aggregates are unaffected by bursts.

## 3. Judge rubric v2 removes the S03 inconsistency; scores still move by tenths

Rubric v1 penalised some tool-less "I cannot set a timer, use your clock app" answers for not performing the action and rewarded others for stating the limitation: on S03 the same behaviour received instruction_following 1–5 across the ten arms. Rubric v2 (`v2-toolless-2026-09-10`) states that the assistant has no tools and that plainly stating the limitation plus the shortest correct manual path fully satisfies the request. Every Task B (470) and Task A (187) answer was re-judged by the same `judge-terra` deployment; the judged iteration is identical to v1 in every cell.

- **S03 (set a timer):** v2 scores all ten arms 5/5 on instruction_following — the identified inconsistency is gone.
- **S04 (open Bluetooth settings):** 8 answers scored 5; the 2 that scored 2 and 3 are the ones whose v2 justification records that the answer gave the path without stating it could not open the page. That is the rule applied as written, with a 1-point spread between two near-identical answers remaining as judge noise.

| Task | Arm | v1 mean | v2 mean | Δ | share of 5.0 (v1 → v2) |
|---|---:|---:|---:|---:|---:|
| B | gpt-5.6-luna-dz | 4.855 | 4.838 | -0.017 | 81% → 81% |
| B | gpt-5.6-luna-dz@low | 4.932 | 4.864 | -0.068 | 83% → 79% |
| B | gpt-5.6-sol-dz | 4.877 | 4.872 | -0.004 | 83% → 89% |
| B | gpt-5.6-sol-dz@low | 4.902 | 4.877 | -0.025 | 87% → 87% |
| B | router-sol-luna-balanced | 4.915 | 4.919 | +0.004 | 85% → 87% |
| B | router-sol-luna-balanced@low | 4.906 | 4.847 | -0.060 | 89% → 81% |
| B | router-sol-luna-cost | 4.911 | 4.877 | -0.034 | 83% → 81% |
| B | router-sol-luna-cost@low | 4.915 | 4.911 | -0.004 | 83% → 77% |
| B | router-sol-luna-quality | 4.906 | 4.872 | -0.034 | 87% → 83% |
| B | router-sol-luna-quality@low | 4.962 | 4.936 | -0.025 | 89% → 94% |
| A | gpt-4o-mini-bench | 4.529 | 4.459 | -0.071 | 47% → 53% |
| A | gpt-5-mini@high | 4.788 | 4.729 | -0.059 | 53% → 59% |
| A | gpt-5-mini@low | 4.635 | 4.459 | -0.176 | 53% → 41% |
| A | gpt-5-mini@medium | 4.765 | 4.718 | -0.047 | 71% → 53% |
| A | gpt-5-mini@minimal | 4.553 | 4.377 | -0.176 | 41% → 35% |
| A | gpt-5.6-luna@high | 4.953 | 4.706 | -0.247 | 82% → 47% |
| A | gpt-5.6-luna@low | 4.824 | 4.765 | -0.059 | 65% → 59% |
| A | gpt-5.6-luna@max | 4.882 | 4.906 | +0.024 | 82% → 71% |
| A | gpt-5.6-luna@medium | 4.918 | 4.788 | -0.129 | 76% → 65% |
| A | gpt-5.6-luna@none | 4.918 | 4.847 | -0.071 | 71% → 82% |
| A | gpt-5.6-luna@xhigh | 4.941 | 4.812 | -0.129 | 76% → 65% |

- Task B: 102/470 cells changed, mean |Δ| 0.135; Task A: 64/187 cells changed, mean |Δ| 0.204. Arm means move by up to 0.25 and several orderings change (v1 also contains exact ties, e.g. two Task B arms at 4.9149): differences of a few hundredths between arms are inside the judge's own variance and must not drive a choice. Full means and both rankings: [judge_rubric_comparison_arms.csv](outputs/judge_rubric_comparison_arms.csv).
- Both rubrics are LLM-as-a-judge without a gold answer; v2 removes one identified inconsistency, it does not make the scores a measure of user-perceived quality. Per-question deltas: [judge_rubric_comparison_questions.csv](outputs/judge_rubric_comparison_questions.csv).

## 4. What changed in the runnable code (also applied in the router folder)

- `harness.py`: SDK `max_retries=0`; a stream that ends without usage is `missing_usage`, not a zero-cost success; Chat `finish_reason` retained and anything other than `stop`/`length` is an error.
- `analyze.py`: the router report applies the same completed/non-truncated/usage-present filter as the direct report and exits on mixed environments.
- `judge.py`: rubric v2 with tool-less rule, `rubric_version` on every row, refuses to overwrite an existing score file, keeps 2 retries (it is not a latency measurement).
- `loadtest.py`: new; stepped concurrency on top of `harness.run_one`.

## 5. Reproduce

```bash
# offline: rebuild every CSV and both READMEs from the retained evidence
python scripts/build_followup_report.py
python -m unittest discover -s tests

# live (paid): same-region Linux VM, Entra identity with Cognitive Services OpenAI User
export AZURE_OPENAI_ENDPOINT="https://<RESOURCE_NAME>.openai.azure.com/"
python harness.py --mode direct --api responses --dataset datasets/router_taskb.jsonl \
  --deployments gpt-5.6-sol-dz,gpt-5.6-luna-dz --region swedencentral --client-location swedencentral-linux-vm \
  --efforts=-,low --iterations 3 --warmup 1
python loadtest.py --dataset datasets/qira_scenarios.jsonl \
  --arms "gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat" \
  --levels 1,4,8,16 --requests-per-level 48 --region swedencentral --client-location swedencentral-linux-vm
python judge.py outputs/<run>.jsonl --judge-deployment judge-terra --dataset datasets/<dataset>.jsonl --out outputs/quality_v2_<run>.jsonl
```

## 6. Evidence

- Full answers: `outputs/raw_fulltext/direct_20260910_074346.jsonl` (752 records), `outputs/raw_fulltext/loadtest_20260910_085128.jsonl`; every record keeps `response_sha256`.
- `outputs/loadtest_20260910_085128.summary.json` written live by `loadtest.py`; the builder recomputes every aggregate from the records and refuses to publish on disagreement.
- `outputs/quality_v2_20260909_223737.jsonl`, `outputs/quality_v2_20260909_120534.jsonl`; v1 inputs and the historical Chat-path direct requests are under `outputs/inputs/` with [manifest.json](outputs/inputs/manifest.json).
- Costs are Global list-price estimates by served model (DataZone premium and router fee excluded); this is not an invoice. Resource state after the run: [resource_closeout_followup.json](outputs/resource_closeout_followup.json).
