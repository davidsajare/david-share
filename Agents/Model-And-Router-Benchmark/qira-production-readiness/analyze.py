#!/usr/bin/env python3
"""
Analysis for the unified harness.

  --mode direct  Task A — per-model quality/latency/token/cost comparison table
  --mode router  Task B — routing distribution per complexity tier + hit rate

Reads the .jsonl written by harness.py. Warmup records are excluded from all
statistics. Cost is recomputed from config/pricing.json at analysis time, so
pricing can be backfilled after a run without re-running the benchmark.

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import argparse
import json
import math
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_PRICING = ROOT / "config" / "pricing.json"
DEFAULT_MODELS = ROOT / "config" / "models.json"

_REGISTRY: dict = {}


def load_registry(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8")).get("models", {})
    return {k.strip().lower(): v for k, v in data.items()}


def load_records(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if not records:
        raise SystemExit(f"{path} contained no records.")
    return records


def load_pricing(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8")).get("models", {})


def load_quality(path: Path | None) -> dict:
    """(arm, question_id) -> quality row, from judge.py output."""
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"quality file not found: {p}")
    scores = {}
    with p.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("quality_mean") is None:
                continue
            arm = row.get("arm") or row.get("model_requested")
            scores[(arm, row.get("question_id"))] = row
    return scores


def pct(values: list[float], q: float):
    """Nearest-rank percentile. Returns None for an empty series."""
    if not values:
        return None
    ordered = sorted(values)
    idx = max(0, min(len(ordered) - 1, math.ceil(q / 100 * len(ordered)) - 1))
    return ordered[idx]


def cost_for(pricing: dict, model: str, prompt: int, cached: int, completion: int):
    """Deployment names may differ from billing model names; resolve via registry."""
    key = (model or "").strip().lower()
    entry = pricing.get(key)
    if not entry:
        alias = (_REGISTRY.get(key, {}) or {}).get("model_name")
        if alias:
            entry = pricing.get(alias.strip().lower())
    if not entry or any(entry.get(k) is None for k in ("input", "cached", "output")):
        return None
    return (max(prompt - cached, 0) / 1_000_000 * entry["input"]
            + cached / 1_000_000 * entry["cached"]
            + completion / 1_000_000 * entry["output"])


def fmt(value, spec: str = ".0f", dash: str = "N/A") -> str:
    return dash if value is None else format(value, spec)


# --------------------------------------------------------------------------
# Fairness guard — same region, same endpoint, no tools, aligned effort
# --------------------------------------------------------------------------

def check_comparability(records: list[dict]) -> bool:
    """
    Refuse to present a comparison table across mixed test conditions.

    Cross-region results are not comparable: network RTT alone moves TTFT by
    100-200ms (see AOAI-Model-Migration-Benchmark, TTFT Composition), which is
    larger than the differences between the models being compared.
    """
    regions = {r.get("region") for r in records if r.get("region")}
    hosts = {r.get("endpoint_host") for r in records if r.get("endpoint_host")}
    dtypes = {r.get("deployment_type") for r in records if r.get("deployment_type")}
    clients = {r.get("client_location") for r in records if r.get("client_location")}
    rtts = {r.get("network_rtt_ms") for r in records if r.get("network_rtt_ms") is not None}
    tooled = [r for r in records if r.get("tools_enabled")]

    print("  Test conditions")
    print("  " + "-" * 100)
    print(f"  region           {', '.join(sorted(regions)) or '(not recorded)'}")
    print(f"  endpoint         {', '.join(sorted(hosts)) or '(not recorded)'}")
    print(f"  deployment type  {', '.join(sorted(dtypes)) or '(not recorded)'}")
    print(f"  load generated   {', '.join(sorted(clients)) or '(not recorded)'}")
    if rtts:
        worst = max(rtts)
        print(f"  TCP connect ms   {', '.join(str(v) for v in sorted(rtts))}")
        print("                   Diagnostic only; not server inference time or proof of region.")
    print(f"  web search       {'ENABLED — INVALID' if tooled else 'disabled (native capability only)'}")

    truncated = [r for r in records if r.get("truncated")]
    if truncated:
        by_arm = Counter(arm_of(r) for r in truncated)
        print(f"  truncated        {len(truncated)}/{len(records)} hit max_output_tokens")
        for a, c in by_arm.most_common():
            print(f"                     {a:<26} {c}")
        print("                   → answers were cut off; raise REASONING_HEADROOM for these")
        print("                     efforts or those arms understate quality and overstate cost")

    efforts = defaultdict(set)
    for r in records:
        efforts[r.get("model_requested")].add(r.get("reasoning_effort"))
    if any(efforts.values()):
        rendered = ", ".join(
            f"{m}={'/'.join(str(e) if e else 'not sent' for e in sorted(v, key=str))}"
            for m, v in sorted(efforts.items(), key=lambda kv: str(kv[0])))
        print(f"  reasoning_effort {rendered}")

    ok = True
    if len(regions) > 1:
        print(f"\n  [FAIL] Records span {len(regions)} regions: {sorted(regions)}")
        print("         Cross-region network RTT differences exceed the model differences")
        print("         being measured. Re-run all models in one region.")
        ok = False
    if len(hosts) > 1:
        print(f"\n  [FAIL] Records span {len(hosts)} endpoints: {sorted(hosts)}")
        print("         Compare models on a single endpoint.")
        ok = False
    if len(dtypes) > 1:
        print(f"\n  [FAIL] Records mix deployment types: {sorted(dtypes)}")
        print("         Capacity and scheduling differ; do not mix these deployment types.")
        ok = False
    if tooled:
        print(f"\n  [FAIL] {len(tooled)} records had tools enabled.")
        print("         This run is not a native-capability comparison.")
        ok = False
    if not regions:
        print("\n  [warn] No region recorded — these records predate the region guard.")

    print()
    return ok


# --------------------------------------------------------------------------
# Task A
# --------------------------------------------------------------------------

def arm_of(r: dict) -> str:
    """Arm identity, falling back for records written before arms existed."""
    if r.get("arm"):
        return r["arm"]
    model = r.get("model_requested") or "?"
    effort = r.get("reasoning_effort")
    return f"{model}@{effort}" if effort else model


def report_direct(records: list[dict], pricing: dict, quality: dict | None = None) -> None:
    quality = quality or {}
    live = [r for r in records if not r["warmup"]]
    ok = [r for r in live if not r.get("error") and r.get("status") == "completed"
          and not r.get("truncated") and (r.get("prompt_tokens") or 0) > 0]

    print("\n" + "=" * 118)
    print("  TASK A — Qira scenario benchmark (S1 Direct, native capability only)")
    print("=" * 118)
    print(f"  {len(live)} measured requests, {len(live) - len(ok)} excluded (errored/incomplete/truncated/no usage)\n")

    comparable = check_comparability(live)
    if not comparable:
        raise SystemExit("Incomparable records: refusing to generate a model comparison.")

    by_model: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_model[arm_of(r)].append(r)

    print("  Prefill / decode / token / cost by arm (model × reasoning_effort)")
    print("  " + "-" * 114)
    header = (f"  {'arm':<24}{'n':>4}{'TTFT P50':>10}{'TTFT P95':>10}"
              f"{'dec t/s':>9}{'E2E P50':>10}"
              f"{'in':>7}{'cached':>8}{'out':>7}{'reason':>8}{'tok/req':>9}{'$/1k req':>11}")
    print(header)
    print("  " + "-" * 114)

    rows = []
    for arm, recs in by_model.items():
        ttfts = [r["ttft_ms"] for r in recs if r.get("ttft_ms") is not None]
        e2es = [r["e2e_ms"] for r in recs]
        tps = [r["decode_tps"] for r in recs if r.get("decode_tps") is not None]
        avg_in = statistics.mean(r["prompt_tokens"] for r in recs)
        avg_cached = statistics.mean(r["cached_tokens"] for r in recs)
        avg_out = statistics.mean(r["completion_tokens"] for r in recs)
        avg_reason = statistics.mean(r.get("reasoning_tokens", 0) or 0 for r in recs)
        model = recs[0].get("model_requested")

        unit = cost_for(pricing, model, round(avg_in), round(avg_cached), round(avg_out))
        rows.append({
            "arm": arm, "model": model, "n": len(recs),
            "ttft50": pct(ttfts, 50), "ttft95": pct(ttfts, 95),
            "tps": statistics.median(tps) if tps else None,
            "e2e50": pct(e2es, 50),
            "in": avg_in, "cached": avg_cached, "out": avg_out, "reason": avg_reason,
            "tok": avg_in + avg_out, "unit": unit,
        })

    rows.sort(key=lambda x: (x["model"] or "", x["ttft50"] if x["ttft50"] is not None else 1e9))

    for x in rows:
        cost = f"${x['unit'] * 1000:.4f}" if x["unit"] is not None else "N/A"
        print(f"  {x['arm']:<24}{x['n']:>4}{fmt(x['ttft50']):>8}ms{fmt(x['ttft95']):>8}ms"
              f"{fmt(x['tps'], '.0f'):>9}{fmt(x['e2e50']):>8}ms"
              f"{x['in']:>7.0f}{x['cached']:>8.0f}{x['out']:>7.0f}{x['reason']:>8.0f}"
              f"{x['tok']:>9.0f}{cost:>11}")

    print("\n  TTFT = prefill + network + queuing.  dec t/s counts emitted text tokens only")
    print("  (reasoning tokens excluded — they are generated before any text surfaces).")
    print("  tok/req = prompt + completion tokens for one single-turn request, not a multi-turn session.")

    if quality:
        report_quality(by_model, quality, pricing)

    # Per Qira scenario. With 11 arms the table is transposed: arms as rows,
    # scenarios as columns, so it stays readable.
    print("\n  By Qira scenario — TTFT P50 ms / avg total tokens per request")
    print("  " + "-" * 114)
    group_key = "scenario" if any(r.get("scenario") for r in ok) else "task_type"
    groups = sorted({r.get(group_key) for r in ok if r.get(group_key)})
    if not groups:
        print("  (dataset carried no scenario or task_type field)")
    else:
        print(f"  {'arm':<24}" + "".join(f"{g[:14]:>15}" for g in groups))
        for arm in sorted(by_model):
            row = f"  {arm:<24}"
            for g in groups:
                sel = [r for r in ok if arm_of(r) == arm and r.get(group_key) == g]
                if not sel:
                    row += f"{'-':>15}"
                    continue
                t = pct([r["ttft_ms"] for r in sel if r.get("ttft_ms") is not None], 50)
                tok = statistics.mean(r["prompt_tokens"] + r["completion_tokens"] for r in sel)
                row += f"{f'{fmt(t)}/{tok:.0f}':>15}"
            print(row)

        partial = sorted({r.get("scenario") for r in ok if r.get("scope") == "partial"})
        if partial:
            print(f"\n  [note] {', '.join(partial)} are text proxies only. Live Interaction's real")
            print("         path runs through Voice Live, and Creator Zone's through image")
            print("         generation — neither is fully characterised by this text benchmark.")

    unpriced = sorted({str(recs[0].get("model_requested")) for recs in by_model.values()
                       if cost_for(pricing, recs[0].get("model_requested"), 1000, 0, 100) is None})
    if unpriced:
        print(f"\n  [!] No confirmed pricing for: {', '.join(unpriced)}")
        print("      Fill config/pricing.json and re-run analyze.py — no need to re-benchmark.")

    if not comparable:
        print("\n  [!] The comparability check above FAILED. Do not publish this table.")


# --------------------------------------------------------------------------
# Task B
# --------------------------------------------------------------------------

TIER_ORDER = ["simple", "moderate", "complex"]

QUALITY_DIMS = ["instruction_following", "accuracy", "completeness", "usefulness", "tone_fit"]


def report_quality(by_model: dict, quality: dict, pricing: dict) -> None:
    """Quality dimension from judge.py, plus the quality-per-dollar view."""
    judges = {q.get("judge_deployment") for q in quality.values()}

    print("\n  Quality (blind LLM-as-a-Judge, 1-5)")
    print("  " + "-" * 100)
    print(f"  judge: {', '.join(sorted(str(j) for j in judges)) or 'unknown'}")
    print(f"  {'arm':<24}{'n':>4}" + "".join(f"{d[:13]:>15}" for d in QUALITY_DIMS)
          + f"{'overall':>10}{'per $/1k':>11}")
    print("  " + "-" * 114)

    for arm, recs in sorted(by_model.items()):
        rows = [q for (a, _), q in quality.items() if a == arm]
        if not rows:
            print(f"  {arm:<24}{0:>4}   (not scored)")
            continue

        cells = ""
        for d in QUALITY_DIMS:
            vals = [r[d] for r in rows if r.get(d) is not None]
            cells += f"{statistics.mean(vals):>15.2f}" if vals else f"{'-':>15}"

        overall = statistics.mean(r["quality_mean"] for r in rows)

        model = recs[0].get("model_requested")
        avg_in = statistics.mean(r["prompt_tokens"] for r in recs)
        avg_cached = statistics.mean(r["cached_tokens"] for r in recs)
        avg_out = statistics.mean(r["completion_tokens"] for r in recs)
        unit = cost_for(pricing, model, round(avg_in), round(avg_cached), round(avg_out))
        ratio = f"{overall / (unit * 1000):.1f}" if unit and unit > 0 else "N/A"

        print(f"  {arm:<24}{len(rows):>4}{cells}{overall:>10.2f}{ratio:>11}")

    print("\n  'per $/1k' is mean quality divided by cost per 1,000 requests — quality per dollar.")
    print("  This descriptive ratio is not a routing rule: quality scores are ordinal.")
    print("  Apply absolute quality and latency acceptance criteria before considering cost.")

    # Where the cheap arm is good enough, per scenario.
    by_scenario: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for (arm, _), q in quality.items():
        if q.get("scenario") and q.get("quality_mean") is not None:
            by_scenario[q["scenario"]][arm].append(q["quality_mean"])

    if by_scenario:
        print("\n  Quality by Qira scenario (arms as rows)")
        print("  " + "-" * 114)
        scens = sorted(by_scenario)
        print(f"  {'arm':<24}" + "".join(f"{s[:14]:>15}" for s in scens))
        for arm in sorted(by_model):
            row = f"  {arm:<24}"
            for s in scens:
                vals = by_scenario[s].get(arm)
                row += f"{statistics.mean(vals):>15.2f}" if vals else f"{'-':>15}"
            print(row)

        print(f"\n  {'scenario':<24}{'spread':>10}   (max - min across arms)")
        for s in scens:
            means = [statistics.mean(v) for v in by_scenario[s].values() if v]
            if len(means) > 1:
                print(f"  {s:<24}{max(means) - min(means):>10.2f}")
        print("\n  A small spread does not prove adequacy: every arm could score poorly.")
        print("  Small-sample judge scores need human calibration before production routing.")


def _is_router(r: dict) -> bool:
    return bool(r.get("router_mode")) or bool(r.get("router_attempts"))


def _served_family(r: dict) -> str | None:
    fam = r.get("served_model_family")
    if fam:
        return fam
    served = r.get("model_actually_served")
    if not served:
        return None
    return re.sub(r"-\d{4}-\d{2}-\d{2}$", "", served)


def _family_sort_key(pricing: dict):
    """Most expensive model first so tables read 'strong tier -> cheap tier'."""
    def key(name: str):
        entry = pricing.get((name or "").lower()) or {}
        return -(entry.get("output") or 0), name
    return key


def report_router(records: list[dict], pricing: dict, quality: dict | None = None) -> None:
    """
    Task B report. A router run contains two kinds of arms:

      router arms   model_requested = a Model Router deployment; the served
                    model and routing trace come from the response
      direct arms   the same models deployed on their own, run in the same
                    session on the same API surface, as latency/cost/quality
                    baselines ("all-Sol" and "all-Luna" policies)

    Every table below is per arm, because balanced/cost/quality are different
    products, and each is judged against the two direct policies.
    """
    quality = quality or {}
    live = [r for r in records if not r["warmup"]]
    # Same validity contract as report_direct: an answer that errored, was cut off, or
    # never reported usage must not enter routing shares, latency or cost tables.
    ok = [r for r in live if not r.get("error") and r.get("status") == "completed"
          and not r.get("truncated") and (r.get("prompt_tokens") or 0) > 0]

    print("\n" + "=" * 118)
    print("  TASK B — Model Router routing validation (2-tier subset: gpt-5.6-sol / gpt-5.6-luna)")
    print("=" * 118)
    print(f"  {len(live)} measured requests, {len(live) - len(ok)} excluded (errored/incomplete/truncated/no usage)\n")

    comparable = check_comparability(live)
    if not comparable:
        raise SystemExit("Incomparable records: refusing to generate a routing comparison.")

    router_ok = [r for r in ok if _is_router(r)]
    direct_ok = [r for r in ok if not _is_router(r)]
    if not router_ok:
        print("  [BLOCKER] No record carries a routing trace or served model from a router deployment.")
        print("  Task B's deliverable is 'record which model actually served each question'.")
        print("  Apply the documented fallback: infer from latency banding and")
        print("  billed token unit price, or read the routing decision from APIM logs.\n")
        return

    families = sorted({_served_family(r) for r in ok if _served_family(r)}, key=_family_sort_key(pricing))
    router_arms = sorted({arm_of(r) for r in router_ok})
    direct_arms = sorted({arm_of(r) for r in direct_ok})
    tiers = [t for t in TIER_ORDER if any(r.get("complexity_tag") == t for r in ok)]
    tiers += sorted({r.get("complexity_tag") for r in ok
                     if r.get("complexity_tag") and r.get("complexity_tag") not in TIER_ORDER})

    modes = sorted({r.get("router_mode") for r in router_ok if r.get("router_mode")})
    print(f"  router modes seen   {modes}")
    print(f"  served families     {families}")
    print(f"  direct baselines    {direct_arms or '(none — run direct Sol/Luna arms in the same session)'}")
    fallbacks = [r for r in router_ok if r.get("router_fallback")]
    print(f"  router fallbacks    {len(fallbacks)}/{len(router_ok)} requests needed a second attempt")
    rl = [r["router_latency_ms"] for r in router_ok if r.get("router_latency_ms") is not None]
    if rl:
        print(f"  router decision     median {statistics.median(rl):.0f}ms, p95 {pct(rl, 95):.0f}ms "
              f"(self-reported routing_trace.latency_ms, n={len(rl)})")

    # ---------------------------------------------------------------- 1. share
    print("\n  1. Routing share by tier — what fraction of requests went to each model")
    print("  " + "-" * 110)
    header = f"  {'arm':<30}{'tier':<11}{'n':>5}   " + "".join(f"{f:>16}" for f in families)
    print(header)
    for arm in router_arms:
        arm_recs = [r for r in router_ok if arm_of(r) == arm]
        for tier in tiers + ["ALL"]:
            sel = arm_recs if tier == "ALL" else [r for r in arm_recs if r.get("complexity_tag") == tier]
            if not sel:
                continue
            counts = Counter(_served_family(r) for r in sel)
            row = f"  {arm:<30}{tier:<11}{len(sel):>5}   "
            for f in families:
                c = counts.get(f, 0)
                row += f"{f'{c} ({c / len(sel) * 100:.0f}%)':>16}"
            print(row)
        print()

    # ------------------------------------------------------------ 2. categories
    print("  2. Which cases switch to the strong tier — routing share by category (all router arms)")
    print("  " + "-" * 110)
    strong = families[0] if families else None
    print(f"  strong tier = {strong}")
    print(f"  {'category':<30}{'tier':<11}{'n':>5}   " + "".join(f"{arm.replace('router-sol-luna-', ''):>14}" for arm in router_arms))
    cats = sorted({(r.get("complexity_tag"), r.get("category")) for r in router_ok},
                  key=lambda t: (TIER_ORDER.index(t[0]) if t[0] in TIER_ORDER else 99, str(t[1])))
    for tier, cat in cats:
        row = f"  {str(cat):<30}{str(tier):<11}"
        n_total = 0
        cells = []
        for arm in router_arms:
            sel = [r for r in router_ok if arm_of(r) == arm
                   and r.get("category") == cat and r.get("complexity_tag") == tier]
            n_total += len(sel)
            if not sel:
                cells.append(f"{'-':>14}")
                continue
            share = sum(1 for r in sel if _served_family(r) == strong) / len(sel)
            cells.append(f"{share:>13.0%} ")
        print(row + f"{n_total:>5}   " + "".join(cells))

    # -------------------------------------------------------------- 3. stability
    print("\n  3. Per-question stability — same prompt, same arm, repeated iterations")
    print("  " + "-" * 110)
    for arm in router_arms:
        by_q: dict[str, list[dict]] = defaultdict(list)
        for r in router_ok:
            if arm_of(r) == arm:
                by_q[r["question_id"]].append(r)
        unstable = []
        for qid, recs in by_q.items():
            counts = Counter(_served_family(r) for r in recs)
            if len(counts) > 1:
                unstable.append((qid, recs[0].get("complexity_tag"), dict(counts)))
        flip_rate = len(unstable) / len(by_q) if by_q else 0
        print(f"  {arm:<30} {len(by_q)} questions, {len(unstable)} routed inconsistently ({flip_rate:.0%})")
        for qid, tier, counts in sorted(unstable):
            print(f"      {qid:<7}{str(tier):<10}{counts}")
    print("  The router is documented as nondeterministic; report hit rates, not a single model per question.")

    # ---------------------------------------------------------------- 4. latency
    print("\n  4. Latency per arm (ms, measured; router arms include the routing decision)")
    print("  " + "-" * 110)
    print(f"  {'arm':<30}{'n':>5}{'TTFT p50':>10}{'TTFT p95':>10}{'e2e p50':>10}{'e2e p95':>10}"
          f"{'client t/s':>12}{'out tok':>9}{'reason tok':>12}")
    print("  Client-delivery token/s estimates are NOT model/GPU generation throughput.")
    print("  Burst delivery and stream buffering can inflate these estimates; TTFT/E2E are client-observed.")
    for arm in router_arms + direct_arms:
        sel = [r for r in ok if arm_of(r) == arm]
        ttft = [r["ttft_ms"] for r in sel if r.get("ttft_ms") is not None]
        e2e = [r["e2e_ms"] for r in sel if r.get("e2e_ms") is not None]
        tps = [r["decode_tps"] for r in sel if r.get("decode_tps") is not None]
        out = [r["completion_tokens"] for r in sel]
        reason = [r["reasoning_tokens"] for r in sel]
        print(f"  {arm:<30}{len(sel):>5}{fmt(statistics.median(ttft) if ttft else None):>10}"
              f"{fmt(pct(ttft, 95)):>10}{fmt(statistics.median(e2e) if e2e else None):>10}"
              f"{fmt(pct(e2e, 95)):>10}{fmt(statistics.median(tps) if tps else None):>12}"
              f"{fmt(statistics.mean(out) if out else None):>9}{fmt(statistics.mean(reason) if reason else None):>12}")

    # Compare routed requests that landed on family F against
    # the direct arm of F at the same effort, on the same questions.
    print("\n  5. Paired TTFT differences — routed-to-F vs direct-F (median of paired deltas)")
    print("  " + "-" * 110)
    direct_by_key: dict[tuple, list[float]] = defaultdict(list)
    for r in direct_ok:
        if r.get("ttft_ms") is not None:
            direct_by_key[(_served_family(r), r.get("reasoning_effort"), r["question_id"])].append(r["ttft_ms"])
    if not direct_by_key:
        print("  (no direct baselines in this run)")
    for arm in router_arms:
        for f in families:
            deltas = []
            for r in router_ok:
                if arm_of(r) != arm or _served_family(r) != f or r.get("ttft_ms") is None:
                    continue
                base = direct_by_key.get((f, r.get("reasoning_effort"), r["question_id"]))
                if base:
                    deltas.append(r["ttft_ms"] - statistics.median(base))
            if deltas:
                print(f"  {arm:<30} → {f:<14} n={len(deltas):<4} median Δ {statistics.median(deltas):>+7.0f}ms"
                      f"   p25 {pct(deltas, 25):>+7.0f}   p75 {pct(deltas, 75):>+7.0f}")
    print("  Positive = higher observed TTFT on the routed path, not attributable router overhead.")
    print("  Global/DataZone serving scope and fixed serial arm ordering can confound this difference.")
    print("  The trace's self-reported latency_ms is a separate component, not an isolated end-to-end cost.")

    # ------------------------------------------------------------------- 6. cost
    print("\n  6. Cost per arm vs the two direct policies (USD per 1k requests, list price, router fee excluded)")
    print("  " + "-" * 110)
    print(f"  {'arm':<30}{'n':>5}{'$/1k req':>12}{'vs all-strong':>15}{'vs all-cheap':>14}   by tier ($/1k)")
    cost_by_arm: dict[str, float] = {}
    effort_of_arm: dict[str, str | None] = {}
    for arm in router_arms + direct_arms:
        sel = [r for r in ok if arm_of(r) == arm]
        effort_of_arm[arm] = sel[0].get("reasoning_effort") if sel else None
        costs = [cost_for(pricing, _served_family(r), r["prompt_tokens"], r["cached_tokens"], r["completion_tokens"])
                 for r in sel]
        costs = [c for c in costs if c is not None]
        if costs:
            cost_by_arm[arm] = statistics.mean(costs) * 1000
    # Direct policies keyed by (family, effort): a router arm at effort E is
    # judged against all-Sol@E and all-Luna@E, never across efforts.
    cheap = families[-1] if families else None
    policy: dict[tuple, float] = {}
    for a in direct_arms:
        fams = {_served_family(r) for r in direct_ok if arm_of(r) == a}
        if len(fams) == 1 and a in cost_by_arm:
            policy[(next(iter(fams)), effort_of_arm[a])] = cost_by_arm[a]
    for arm in router_arms + direct_arms:
        if arm not in cost_by_arm:
            print(f"  {arm:<30} N/A (pricing missing for served models)")
            continue
        c = cost_by_arm[arm]
        policy_strong = policy.get((strong, effort_of_arm[arm]))
        policy_cheap = policy.get((cheap, effort_of_arm[arm]))
        vs_s = f"{(c / policy_strong - 1):+.0%}" if policy_strong else "-"
        vs_c = f"{(c / policy_cheap - 1):+.0%}" if policy_cheap else "-"
        tier_txt = []
        for tier in tiers:
            sel = [r for r in ok if arm_of(r) == arm and r.get("complexity_tag") == tier]
            tc = [cost_for(pricing, _served_family(r), r["prompt_tokens"], r["cached_tokens"], r["completion_tokens"])
                  for r in sel]
            tc = [x for x in tc if x is not None]
            if tc:
                tier_txt.append(f"{tier[0].upper()}={statistics.mean(tc) * 1000:.2f}")
        n = sum(1 for r in ok if arm_of(r) == arm)
        print(f"  {arm:<30}{n:>5}{c:>12.3f}{vs_s:>15}{vs_c:>14}   {' '.join(tier_txt)}")
    print("  'all-strong' = direct Sol arm at the same effort; 'all-cheap' = direct Luna arm.")
    print("  Model Router's own per-prompt fee is not on the rendered pricing page and is excluded.")

    # ---------------------------------------------------------------- 7. quality
    if quality:
        print("\n  7. Quality per arm (blind judge, 1-5, one iteration per arm×question)")
        print("  " + "-" * 110)
        print(f"  {'arm':<30}{'n':>5}{'mean':>7}   " + "".join(f"{d[:12]:>13}" for d in QUALITY_DIMS))
        for arm in router_arms + direct_arms:
            rows = [q for (a, _qid), q in quality.items() if a == arm]
            if not rows:
                continue
            means = [q["quality_mean"] for q in rows]
            dims = []
            for d in QUALITY_DIMS:
                vals = [q.get(d) for q in rows if isinstance(q.get(d), (int, float))]
                dims.append(f"{statistics.mean(vals):>13.2f}" if vals else f"{'-':>13}")
            print(f"  {arm:<30}{len(rows):>5}{statistics.mean(means):>7.2f}   " + "".join(dims))
        # Quality split by where the router sent the request
        print("\n     routed requests, split by served family:")
        for arm in router_arms:
            for f in families:
                qids = {r["question_id"] for r in router_ok if arm_of(r) == arm and _served_family(r) == f}
                rows = [q for (a, qid), q in quality.items() if a == arm and qid in qids]
                if rows:
                    print(f"     {arm:<30} → {f:<14} n={len(rows):<4} mean {statistics.mean(q['quality_mean'] for q in rows):.2f}")

    if not comparable:
        print("\n  [!] The comparability check above FAILED. Do not publish these tables.")


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(description="Analyze harness.py output.")
    p.add_argument("results", help="path to an outputs/*.jsonl file")
    p.add_argument("--mode", choices=["direct", "router", "auto"], default="auto")
    p.add_argument("--pricing", default=str(DEFAULT_PRICING))
    p.add_argument("--models", default=str(DEFAULT_MODELS),
                   help="model registry, used to map deployment names to billing model names")
    p.add_argument("--quality", default=None,
                   help="path to a quality_*.jsonl from judge.py, to add the quality dimension")
    args = p.parse_args()

    records = load_records(Path(args.results))
    pricing = load_pricing(Path(args.pricing))
    global _REGISTRY
    _REGISTRY = load_registry(Path(args.models))
    quality = load_quality(args.quality)

    mode = args.mode
    if mode == "auto":
        mode = records[0].get("route_mode", "direct")

    if mode == "router":
        report_router(records, pricing, quality)
    else:
        report_direct(records, pricing, quality)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
