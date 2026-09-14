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
    ok = [r for r in live if not r.get("error")]

    print("\n" + "=" * 118)
    print("  TASK A — Qira scenario benchmark (S1 Direct, native capability only)")
    print("=" * 118)
    print(f"  {len(live)} measured requests, {len(live) - len(ok)} errored\n")

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


def report_router(records: list[dict], pricing: dict) -> None:
    live = [r for r in records if not r["warmup"]]
    ok = [r for r in live if not r.get("error")]

    print("\n" + "=" * 112)
    print("  TASK B — Model Router routing validation")
    print("=" * 112)
    print(f"  {len(live)} measured requests, {len(live) - len(ok)} errored\n")

    check_comparability(live)

    requested = {r["model_requested"] for r in ok}
    served = {r["model_actually_served"] for r in ok if r["model_actually_served"]}

    if not served:
        print("  [BLOCKER] No response exposed a served model name.")
        print("  Task B's deliverable is 'record which model actually served each question'.")
        print("  Fallback: infer the served model from latency banding and")
        print("  billed token unit price, or read the routing decision from APIM logs.\n")
        return

    if served <= requested:
        print("  [WARN] Every response echoed the requested deployment name")
        print(f"         ({sorted(requested)}). The router is likely not exposing its")
        print("         underlying choice. Verify before trusting the table below.\n")

    print("  Routing distribution by complexity tier")
    print("  " + "-" * 100)
    models = sorted(served)
    print(f"  {'tier':<12}{'n':>5}   " + "".join(f"{m:>24}" for m in models))

    tiers = [t for t in TIER_ORDER if any(r.get("complexity_tag") == t for r in ok)]
    tiers += sorted({r.get("complexity_tag") for r in ok
                     if r.get("complexity_tag") and r.get("complexity_tag") not in TIER_ORDER})

    for tier in tiers:
        sel = [r for r in ok if r.get("complexity_tag") == tier]
        counts = Counter(r["model_actually_served"] for r in sel)
        row = f"  {tier:<12}{len(sel):>5}   "
        for m in models:
            c = counts.get(m, 0)
            share = c / len(sel) * 100 if sel else 0
            row += f"{f'{c} ({share:.0f}%)':>24}"
        print(row)

    # Per-question stability — the router is known to be nondeterministic, so
    # report a hit rate rather than a single winning model.
    print("\n  Per-question routing stability")
    print("  " + "-" * 100)
    print(f"  {'qid':<7}{'tier':<11}{'n':>4}  {'dominant model':<26}{'stability':>10}   distribution")

    unstable = 0
    by_q: dict[str, list[dict]] = defaultdict(list)
    for r in ok:
        by_q[r["question_id"]].append(r)

    for qid in sorted(by_q):
        recs = by_q[qid]
        counts = Counter(r["model_actually_served"] for r in recs)
        top, top_n = counts.most_common(1)[0]
        stability = top_n / len(recs)
        if stability < 1.0:
            unstable += 1
        dist = " ".join(f"{m}:{c}" for m, c in counts.most_common())
        print(f"  {qid:<7}{str(recs[0].get('complexity_tag')):<11}{len(recs):>4}  "
              f"{str(top):<26}{stability:>9.0%}   {dist}")

    print(f"\n  {unstable}/{len(by_q)} questions routed inconsistently across iterations.")
    if unstable:
        print("  Report routing as a hit-rate distribution, not a single model per question.")

    # Cost split across the tiers, which is the actual argument for routing.
    print("\n  Cost by tier (recomputed from pricing.json)")
    print("  " + "-" * 100)
    any_cost = False
    for tier in tiers:
        sel = [r for r in ok if r.get("complexity_tag") == tier]
        costs = [cost_for(pricing, r["model_actually_served"], r["prompt_tokens"],
                          r["cached_tokens"], r["completion_tokens"]) for r in sel]
        costs = [c for c in costs if c is not None]
        if not costs:
            print(f"  {tier:<12} N/A (pricing not filled in for the served models)")
            continue
        any_cost = True
        print(f"  {tier:<12} avg ${statistics.mean(costs) * 1000:.4f} / 1k requests"
              f"   (n={len(costs)})")
    if not any_cost:
        print("  Fill config/pricing.json to produce the cost-routing argument.")


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
        report_router(records, pricing)
    else:
        report_direct(records, pricing, quality)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
