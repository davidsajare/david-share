# Lenovo Qira — Production readiness: sessions, sustained load, fallback, lifecycle

The three earlier studies ([scenario benchmark](../qira-scenario-model-benchmark/README.md), [router validation](../qira-model-router-validation/README.md), [follow-up](../qira-followup-throughput-recalibration/README.md)) measured single turns at concurrency 1 and said so. This one closes the four gaps a production decision still leaves open: **multi-turn session cost and latency**, **steady-state throughput under sustained concurrency**, **behaviour at the PAYGO rate limit with and without a fallback**, and **model lifecycle / API compatibility**. Same Sweden Central VM and resource, no search or tools, hardened harness (`max_retries=0`, usage required).

> Author: **Xinyu Wei (魏新宇)** · 2026-09-11 · runs `sessions_20260911_015218`, `sustained_20260911_020626`, `resilience_20260911_024156`; lifecycle queried 2026-09-11

[English](README.md) | [中文](README-CN.md)

## Decision summary

- **Budget Qira per conversation, not per turn — and the two are not far apart on these scripts.** A 4-turn session cost 0.90–1.14× four first turns: re-sending the history grew prompt tokens from turn 1 to turn 4 on every arm (table below), but the scripted follow-up turns are capped shorter than the opener (400 → 200 → 200 → 80 output tokens in `datasets/qira_sessions.jsonl`, a design choice), so the two effects roughly cancel here. Per 1,000 sessions: gpt-4o-mini-bench $0.44 … router-sol-luna-quality $11.43. **Prompt caching contributed nothing: 0/360 turns reported cached tokens.** Caching needs a ≥ 1,024-token prefix that an earlier request already sent; the longest prompt that was later re-sent as a prefix was 924 tokens. Longer conversations or long system prompts would cross that line — not measured here.
- **Model Router in `quality` mode changed model inside 18 of 18 conversations; in `balanced` mode 0 of 18.** Quality mode's Sol share rose with the conversation: 33% → 33% → 67% → 83% from turn 1 to turn 4, and its observed per-session sequences were `luna>luna>sol>sol`, `luna>sol>luna>sol`, `luna>sol>sol>sol`, `sol>luna>luna>luna`, `sol>luna>sol>sol`. A Qira user on quality mode would be answered by different models within one conversation — tone and formatting can shift turn to turn, and the per-turn cost is unpredictable. Balanced mode served Luna for every turn. 18 sessions per mode; treat as an observed pattern, not a rule.
- **Sustained load:** with continuous refill (target concurrency held for 100% of every counting window), the three candidates at capacity 1000 completed every request at 4/8/16 in flight; the balanced router at capacity 300 returned **237 × 429 at 16 in flight** (0 at 8, 0 at 4). Capacity, not the model, sets the ceiling.
- **Fallback is mandatory on PAYGO, and it must watch the stream, not just the status code.** Driving a capacity-10 Luna deployment at 8 in flight, a client with no fallback succeeded on **2.7%** of requests. Every rate-limit rejection observed across the three policies arrived **after an HTTP 200, as an error event inside the stream**: 473 in the counted windows (585 including ramp and deadline-cut records), 0 at request time, 0 HTTP 429s. A first version of the fallback client that only retried on request-time status — the semantics of APIM's `<retry condition="429">` — therefore never triggered and matched the no-fallback result (evidence kept in `outputs/resilience_v1_request_time_only/`). The corrected client switches backend on an error seen before any content is delivered: reactive succeeded on **100.0%** (108 in-stream switches), adding 411 ms P50 / 1035 ms P90 to TTFT on rescued requests; proactive (route to the secondary while primary utilization ≥ 80 %, 60 s cache) succeeded on **100.0%** with 122 requests never touching the primary.
- **Lifecycle is a selection criterion, not a footnote:** per the Models API on 2026-09-11, **gpt-5-mini 2025-08-07 retires 2027-02-09** and **gpt-4o-mini 2024-07-18 is already deprecated for new customers** (retires 2027-04-14); the GPT-5.6 family runs to 2028-01-11. `model-router` advertises `chatCompletion` only — consistent with the Responses-path rejection observed in #2.

## 1. Multi-turn sessions: what a conversation costs

Six scripted conversations, one per Qira scenario, four dependent turns each (turn k asks about the model's own answer to turn k−1). Replayed statelessly — system message, prior user/assistant pairs, new user message — identically on Responses (three candidates) and Chat Completions (two router modes); 3 iterations per session; 360 turns, all completed. Cost = Global list price by served model.

| Arm | Prompt tokens T1 → T4 | Turns with cache hit | TTFT P50 T1 → T4 s | Wait per session (ΣTTFT) P50 s | USD / 1,000 sessions | Session ÷ 4×T1 | Router: Sol turns / sessions that changed model |
|---|---:|---:|---:|---:|---:|---:|---:|
| gpt-4o-mini-bench | 120 → 553 | 0/72 | 0.35 → 0.43 | 1.69 | 0.441 | 1.14× | — |
| gpt-5-mini@minimal | 119 → 890 | 0/72 | 0.78 → 0.63 | 3.06 | 2.212 | 0.90× | — |
| gpt-5.6-luna@none | 119 → 640 | 0/72 | 0.94 → 0.73 | 3.10 | 0.910 | 1.03× | — |
| router-sol-luna-balanced | 119 → 686 | 0/72 | 1.91 → 1.31 | 5.89 | 1.128 | 0.98× | 0% / 0 |
| router-sol-luna-quality | 119 → 680 | 0/72 | 1.93 → 1.60 | 6.69 | 11.433 | 1.11× | 54% / 18 |

- Per-turn detail (prompt/cached/output tokens, TTFT/E2E, cost, Sol share by turn): [sessions_by_turn.csv](outputs/sessions_by_turn.csv); per-session router sequences: [sessions_router_sequences.csv](outputs/sessions_router_sequences.csv).
- "Session ÷ 4×T1" compares the measured 4-turn cost with four first turns. Below 1 means the follow-up turns were cheaper than the opener despite carrying the history; it is specific to these scripts, and conversations with long openers re-sent many times grow faster than linearly.
- Zero cache hits is expected behaviour, not a defect: no request in these sessions carried a ≥ 1,024-token prefix that a previous request had already sent (`max_reusable_prefix_tokens` in [sessions_by_arm.csv](outputs/sessions_by_arm.csv)).

## 2. Sustained load: steady state at 4 / 8 / 16 in flight

Each level runs for 90 s with exactly N workers that start the next prompt the moment one finishes; the first 15 s are ramp and only requests that start after the ramp and finish before the deadline are counted (the *share of window at target* column, recomputed from per-request offsets, is how long N were actually in flight). Prompts cycle the 17 Qira scenario prompts. This replaces the closed-batch table in #3, whose ≤16 rows held the target concurrency for only 22–45 % of the wall.

| Arm | N | Completed | 429 | Window at N | req/s | Out tok/s (incl. reasoning) | Visible tok/s | TTFT P50 / P90 / P95 s | E2E P50 / P90 s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| gpt-4o-mini-bench | 4 | 114/114 | 0 | 100% | 1.52 | 196 | 196 | 0.34 / 0.81 / 1.47 | 1.75 / 4.79 |
| gpt-4o-mini-bench | 8 | 251/251 | 0 | 100% | 3.35 | 435 | 435 | 0.34 / 0.63 / 0.92 | 1.74 / 4.31 |
| gpt-4o-mini-bench | 16 | 506/506 | 0 | 100% | 6.75 | 876 | 876 | 0.34 / 0.66 / 1.00 | 1.64 / 4.20 |
| gpt-5-mini@minimal | 4 | 90/90 | 0 | 100% | 1.20 | 315 | 315 | 0.53 / 1.30 / 2.13 | 2.04 / 5.61 |
| gpt-5-mini@minimal | 8 | 191/191 | 0 | 100% | 2.55 | 700 | 700 | 0.53 / 1.17 / 1.42 | 1.94 / 4.82 |
| gpt-5-mini@minimal | 16 | 391/391 | 0 | 100% | 5.21 | 1416 | 1416 | 0.50 / 1.27 / 2.09 | 2.06 / 4.57 |
| gpt-5.6-luna@none | 4 | 86/86 | 0 | 100% | 1.15 | 228 | 228 | 0.79 / 1.12 / 1.31 | 1.85 / 3.62 |
| gpt-5.6-luna@none | 8 | 161/161 | 0 | 100% | 2.15 | 474 | 474 | 0.80 / 1.09 / 1.27 | 1.86 / 3.67 |
| gpt-5.6-luna@none | 16 | 322/322 | 0 | 100% | 4.29 | 939 | 939 | 0.77 / 1.20 / 1.76 | 1.87 / 3.88 |
| router-sol-luna-balanced | 4 | 83/83 | 0 | 100% | 1.11 | 293 | 256 | 1.65 / 2.81 / 3.10 | 2.44 / 3.79 |
| router-sol-luna-balanced | 8 | 177/177 | 0 | 100% | 2.36 | 620 | 530 | 1.62 / 2.82 / 3.41 | 2.29 / 3.94 |
| router-sol-luna-balanced | 16 | 349/586 | 237 | 100% | 4.65 | 1241 | 1072 | 1.56 / 2.53 / 2.84 | 2.26 / 3.76 |

| Arm | Visible tok/s 4 → 16 | req/s 4 → 16 | TTFT P50 4 → 16 s | TTFT P90 4 → 16 s | 429 at 4 / 8 / 16 |
|---|---:|---:|---:|---:|---:|
| gpt-4o-mini-bench | 196 → 876 | 1.52 → 6.75 | 0.34 → 0.34 | 0.81 → 0.66 | 0 / 0 / 0 |
| gpt-5-mini@minimal | 315 → 1416 | 1.20 → 5.21 | 0.53 → 0.50 | 1.30 → 1.27 | 0 / 0 / 0 |
| gpt-5.6-luna@none | 228 → 939 | 1.15 → 4.29 | 0.79 → 0.77 | 1.12 → 1.20 | 0 / 0 / 0 |
| router-sol-luna-balanced | 256 → 1072 | 1.11 → 4.65 | 1.65 → 1.56 | 2.81 → 2.53 | 0 / 0 / 237 |

- 237 × 429 and 0 other errors in total, all on the router deployment (capacity 300); the 237 rejection payloads carry the router's own trace: `x-ratelimit-limit-requests: 300`, `Retry-After` 1–3 s, and **exactly one attempt each** (gpt-5.6-luna→429: 237; attempts-per-request histogram {1: 237}) — the router did not retry on Sol when its Luna attempt was throttled. The capacity-1000 candidates did not reach their limits at 16 in flight with these prompt lengths. A production capacity plan has to start from the deployment's RPM/TPM setting and the observed req/s per concurrent user, not from the model.
- The router's aggregate includes the reasoning tokens its Luna runs with by default (no effort sent); compare it on the *visible* column against `gpt-5.6-luna@none`.
- Single deployment, single region, one client: this is the deployment's steady state on this day, not an SLA. Per-request records with offsets (answer text replaced by its SHA256, see §6): [outputs/raw_fulltext/sustained_20260911_020626.numeric.jsonl](outputs/raw_fulltext/sustained_20260911_020626.numeric.jsonl).

## 3. Rate limit and fallback: three client policies against a throttled primary

`gpt-5.6-luna-lowcap` is the same gpt-5.6-luna model deployed at DataZoneStandard capacity 10 (service rate limits: 10 requests/min, 10,000 tokens/min), so the PAYGO limit is reachable at trivial cost. `gpt-5.6-luna` (GlobalStandard, capacity 1000) is the secondary. 8 workers for 75 s per policy (15 s ramp excluded), 1 s pause after a failed request, 70 s between policies so the primary's minute window resets. The client is [fallback_client.py](fallback_client.py). It takes the two ideas of the reference APIM policy in [config/apim-policy-ptu-routing.reference.xml](config/apim-policy-ptu-routing.reference.xml) — retry on a second backend when the first fails, and route ahead of the limit from the `x-ratelimit-*` headers — and **extends both**: it switches on an in-stream error before the first content delta (the XML's `<retry condition="429">` only sees the HTTP status, which was 200 here), and its utilization is the worse of *request* and *token* utilization (the XML reads tokens only; on this RPM-bound deployment token utilization stayed near 10 %, so that policy as written would not have gone proactive). Hosting the same logic in API Management therefore needs a policy revised on both points; it was not deployed or tested here.

| Policy | Completed | Success | Rate-limit failed | Served by primary | Served by fallback | In-stream switches | Proactive skips | TTFT P50 primary s | TTFT P50 fallback s | Fallback overhead P50 / P90 ms | req/s |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| none | 10/375 | 2.7% | 365 | 10 | 0 | 0 | 0 | 0.44 | - | - | 0.17 |
| reactive | 118/118 | 100.0% | 0 | 10 | 108 | 108 | 0 | 0.48 | 1.03 | 411 / 1035 | 1.97 |
| proactive | 131/131 | 100.0% | 0 | 9 | 122 | 0 | 122 | 0.45 | 0.76 | - | 2.18 |

- **The shape of the rejection matters more than the number.** On the streaming Responses path the DataZone deployment accepted every request (HTTP 200) and then emitted `Your requests to gpt-5.6-luna for gpt-5.6-luna-lowcap in swedencentral have exceeded rate limit.` as an in-stream error; because the HTTP exchange had already succeeded there is no `Retry-After` header for the client to read. The Chat Completions router deployment in section 2 rejected with a real HTTP 429 (`Retry-After: 3`). A gateway or client that only inspects the response status will pass the in-stream case through to the user as a failed answer; the retry has to happen at the first stream event. This is the single most important finding of the run for anyone wiring Qira to PAYGO.
- **What the customer should take from this:** a second backend plus a client (or gateway) that switches on *any* failure before the first content delta. Without it, 97.3% of requests in this run failed outright. With it, the 92% of requests that fell back saw TTFT 1.03 s P50 = the primary's rejection latency (411 ms P50) + the secondary's own first token (0.55 s P50 net of that overhead; the secondary undisturbed measured 0.76 s under proactive routing and 0.80 s in section 2 at 8 in flight).
- **Proactive routing removes the rejection latency** for requests routed straight to the secondary, at the price of reading rate-limit headers on every primary response and re-probing the primary when the 60 s cache expires (the reference policy's `duration="60"`). Whether that is worth 411 ms depends on the product's TTFT budget.
- **Model Router does not fall back on a deployment-level 429.** The 237 router rejections in section 2 each carry a trace with a single attempt (`gpt-5.6-luna → 429`) and no retry on Sol; across 1,128 routed requests in #2 and the router runs here, the trace never showed a second attempt. Its behaviour when an underlying model itself is down remains **not observed**. Either way, the client-side fallback above is what handles it.
- Same model on both sides of the chain, so quality is unchanged by fallback; a cross-model fallback (e.g. to gpt-4o-mini) uses the same mechanism but changes the answer — score it before adopting it.

## 4. Lifecycle and API compatibility (Models API, not prose)

Queried with [scripts/query_model_lifecycle.py](scripts/query_model_lifecycle.py) on 2026-09-11; raw answer in [model_lifecycle_swedencentral.json](outputs/model_lifecycle_swedencentral.json). `Deprecated` here means the documented state "existing customers only" (API value `Deprecating`); dates are the service's programmatic values on that day and can move. Standard lifecycle is 18 months GA → deprecated at 12 → retired.

| Model | Version | Lifecycle | Inference retirement | API capabilities | Deployment SKUs | Role in these studies |
|---|---:|---:|---:|---:|---:|---:|
| gpt-5-mini | 2025-08-07 | GA | 2027-02-09 | chatCompletion, responses, assistants | DataZoneProvisionedManaged, DataZoneStandard, GlobalProvisionedManaged, GlobalStandard, ProvisionedManaged | Task A candidate |
| gpt-4o-mini | 2024-07-18 | Deprecated (existing customers only) | 2027-04-14 | chatCompletion, responses, assistants, jsonObjectResponse, fineTune | DataZoneProvisionedManaged, DataZoneStandard, GlobalProvisionedManaged, GlobalStandard, ProvisionedManaged, Standard | Task A candidate (gpt-4o-mini-bench) |
| model-router | 2025-11-18 | GA | 2027-05-20 | chatCompletion | DataZoneStandard, GlobalStandard | Task B router deployments |
| gpt-5.6-luna | 2026-07-09 | GA | 2028-01-11 | chatCompletion, responses, assistants | DataZoneStandard, GlobalProvisionedManaged, GlobalStandard | Task A candidate; Task B fast tier; fallback target |
| gpt-5.6-sol | 2026-07-09 | GA | 2028-01-11 | chatCompletion, responses, assistants | DataZoneProvisionedManaged, DataZoneStandard, GlobalProvisionedManaged, GlobalStandard | Task B strong tier |
| gpt-5.6-terra | 2026-07-09 | GA | 2028-01-11 | chatCompletion, responses, assistants | DataZoneProvisionedManaged, DataZoneStandard, GlobalProvisionedManaged, GlobalStandard | blind judge (judge-terra) |

- For a workload going live in Q4 2026, gpt-5-mini 2025-08-07 leaves under six months of runway and gpt-4o-mini 2024-07-18 can no longer be deployed by a subscription that has not used it before. Both were the cheap/fast candidates in #1; their price-performance has to be weighed against a forced migration inside the first year.
- All three candidate models and Sol expose `chatCompletion`, `responses` and `assistants`; `model-router` exposes `chatCompletion` only. In the Sweden Central listing gpt-5.6-luna is not offered on regional `ProvisionedManaged` (only Global/DataZone Standard and Global Provisioned) — relevant if the PTU discussion resumes; SKU availability is region-scoped and should be re-queried for the target region.

## 5. Reproduce

```bash
# offline: rebuild every CSV and both READMEs from the retained evidence; run the tests
python scripts/build_readiness_report.py
python -m unittest discover -s tests

# live (paid): same-region Linux VM, Entra identity with Cognitive Services OpenAI User on the resource
export AZURE_OPENAI_ENDPOINT="https://<RESOURCE_NAME>.openai.azure.com/"
python sessions.py --dataset datasets/qira_sessions.jsonl \
  --arms "gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat,router-sol-luna-quality#chat" \
  --iterations 3 --region swedencentral --client-location swedencentral-linux-vm
python sustained_load.py --dataset datasets/qira_scenarios.jsonl \
  --arms "gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat" \
  --levels 4,8,16 --duration 90 --ramp 15 --region swedencentral --client-location swedencentral-linux-vm
# create a capacity-10 deployment of the primary model first (az cognitiveservices account deployment create ... --sku-capacity 10)
python resilience_test.py --dataset datasets/qira_scenarios.jsonl --primary gpt-5.6-luna-lowcap --fallback gpt-5.6-luna \
  --effort none --policies none,reactive,proactive --level 8 --duration 75 --ramp 15 --region swedencentral --client-location swedencentral-linux-vm
python scripts/query_model_lifecycle.py --region swedencentral
```

## 6. Evidence and boundaries

- Full answers with `response_sha256`: `outputs/raw_fulltext/sessions_20260911_015218.jsonl` and `resilience_20260911_024156.jsonl`. The sustained run is retained **numerically** (`sustained_20260911_020626.numeric.jsonl`: every field except `response_text`, whose SHA256 is kept per record); its 3,770 answers to the same 17 prompts remain on the deallocated VM disk (`sustained_fulltext_retained_on_vm_only.sha256` in the provenance) and were not moved through the management-plane transfer channel. Live summaries written by the runners are cross-checked by the builder and kept beside the records. First fallback attempt (request-time-only client) kept under `outputs/resilience_v1_request_time_only/`. Hashes in [provenance_readiness.json](outputs/provenance_readiness.json); resource state after the run in [resource_closeout_readiness.json](outputs/resource_closeout_readiness.json).
- Synthetic English prompts and scripted conversations, not customer traffic; one client VM; TTFT/E2E are client-observed and include network and delivery; costs are Global list-price estimates by served model (DataZone premium and router fee excluded), not invoices.
- Not measured: conversations long enough to trigger prompt caching; concurrency beyond 16 or durations beyond 90 s; the 429 onset of the capacity-1000 deployments; whether the in-stream rate-limit shape also applies to GlobalStandard deployments or to Chat Completions on a direct model (the router deployment on Chat returned HTTP 429); Model Router behaviour when an underlying model is unavailable; cross-model fallback quality; API Management itself (the reference policy was neither deployed nor executed — the in-process client reproduces its intent with the two extensions described in §3).
