#!/usr/bin/env python3
"""
Steady-state sustained-load generator (fixed duration, continuous refill).

The follow-up study's loadtest.py submitted closed 48-request batches: in-flight
equalled the target level only until the queue drained, so its "≤N in flight"
aggregates were dominated by the longest answers finishing alone. This runner
keeps exactly N workers busy for a fixed duration: whenever a request finishes
the worker immediately starts the next prompt, so the in-flight count stays at N
until the deadline.

Counting rule: only requests that STARTED after the ramp period and FINISHED
before the deadline are counted; requests still in flight at the deadline are
recorded with counted=false. Aggregate throughput = counted output tokens ÷
(deadline − ramp end). Per-request TTFT/E2E/usage are harness.run_one, unchanged.

Usage (same-region VM):
    python sustained_load.py --dataset datasets/qira_scenarios.jsonl \
        --arms gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat \
        --levels 4,8,16 --duration 90 --ramp 15 --region swedencentral \
        --client-location swedencentral-linux-vm

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import harness

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs"


def pct(values, q):
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(q / 100 * len(ordered)) - 1))]


def parse_arm(spec: str) -> dict:
    api = "responses"
    if "#" in spec:
        spec, api = spec.split("#", 1)
    deployment, effort = (spec.split("@", 1) + [None])[:2]
    if effort in ("-", ""):
        effort = None
    if deployment.startswith("router-"):
        api = "chat"
    return {"deployment": deployment, "effort": effort, "api": api,
            "arm_id": f"{deployment}@{effort}" if effort else deployment}


def classify_error(error: str | None) -> str | None:
    if not error:
        return None
    if "RateLimitError" in error or " 429" in error or "exceeded rate limit" in error.lower():
        return "429"
    if "Timeout" in error:
        return "timeout"
    if "APIConnectionError" in error:
        return "connection"
    if "InternalServerError" in error or " 5" in error[:40]:
        return "5xx"
    return "other"


def run_level(client, arm, dataset, level, duration, ramp, args, fh, lock, stamp, endpoint_host, rtt, pricing, registry):
    """Keep `level` workers refilling for `duration` seconds; return records for this level."""
    t0 = time.perf_counter()
    deadline = t0 + duration
    ramp_end = t0 + ramp
    records = []
    next_index = [0]
    index_lock = threading.Lock()

    def worker(worker_id):
        while True:
            now = time.perf_counter()
            if now >= deadline:
                return
            with index_lock:
                i = next_index[0]
                next_index[0] += 1
            item = dataset[i % len(dataset)]
            cap = int(item.get("max_output_tokens", args.max_output_tokens)) + harness.REASONING_HEADROOM_UNIFORM
            started = time.perf_counter()
            m = harness.run_one(client, arm["deployment"], item, cap, arm["effort"], api=arm["api"])
            finished = time.perf_counter()
            served = m["model_actually_served"]
            billing = harness.billing_model_name(served)
            routing = m.get("routing") or {}
            counted = started >= ramp_end and finished <= deadline
            record = {
                "run_id": stamp, "kind": "sustained", "arm": arm["arm_id"], "model_requested": arm["deployment"],
                "reasoning_effort": arm["effort"], "api": arm["api"], "level": level, "worker": worker_id,
                "sequence": i, "question_id": item["id"], "scenario": item.get("scenario"),
                "region": args.region, "endpoint_host": endpoint_host, "deployment_type": args.deployment_type,
                "client_location": args.client_location, "network_rtt_ms": rtt, "tools_enabled": False, "warmup": False,
                "started_offset_ms": round((started - t0) * 1000, 1), "finished_offset_ms": round((finished - t0) * 1000, 1),
                "in_ramp": started < ramp_end, "cut_by_deadline": finished > deadline, "counted": counted,
                "model_actually_served": served, "served_model_family": billing,
                "router_mode": routing.get("mode"), "router_latency_ms": routing.get("router_latency_ms"),
                "error_class": classify_error(m["error"]),
                "cost_usd": harness.compute_cost(pricing, billing or arm["deployment"], m["prompt_tokens"],
                                                 m["cached_tokens"], m["completion_tokens"], registry),
                "response_chars": len(m["response_text"]),
                "response_sha256": hashlib.sha256(m["response_text"].encode()).hexdigest(),
                **{k: m[k] for k in ("ttft_ms", "e2e_ms", "decode_ms", "tpot_ms", "decode_tps", "prompt_tokens",
                                     "cached_tokens", "completion_tokens", "reasoning_tokens", "status", "truncated",
                                     "incomplete_reason", "error")},
                "finish_reason": m.get("finish_reason"),
                "response_text": m["response_text"],
            }
            with lock:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                records.append(record)

    threads = [threading.Thread(target=worker, args=(w,), daemon=True) for w in range(level)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return records, ramp_end - t0, deadline - t0


def summarize(arm, level, records, ramp_s, deadline_s, duration):
    counted = [r for r in records if r["counted"]]
    ok = [r for r in counted if not r["error"] and r["status"] == "completed" and not r["truncated"]]
    errors = [r for r in counted if r["error"]]
    window = deadline_s - ramp_s
    ttft = [r["ttft_ms"] for r in ok if r["ttft_ms"] is not None]
    e2e = [r["e2e_ms"] for r in ok]
    out_tokens = sum(r["completion_tokens"] for r in ok)
    return {
        "arm": arm["arm_id"], "api": arm["api"], "level": level, "duration_s": duration, "ramp_s": ramp_s,
        "window_s": round(window, 1), "all_records": len(records), "counted": len(counted),
        "cut_by_deadline": sum(1 for r in records if r["cut_by_deadline"]), "in_ramp": sum(1 for r in records if r["in_ramp"]),
        "ok": len(ok), "errors": len(errors), "errors_429": sum(1 for r in errors if r["error_class"] == "429"),
        "other_errors": sum(1 for r in errors if r["error_class"] != "429"),
        "truncated": sum(1 for r in counted if r["truncated"]),
        "requests_per_s": round(len(ok) / window, 3),
        "aggregate_output_tok_per_s": round(out_tokens / window, 1),
        "aggregate_visible_tok_per_s": round(sum(r["completion_tokens"] - r["reasoning_tokens"] for r in ok) / window, 1),
        "reasoning_share_of_output": round(sum(r["reasoning_tokens"] for r in ok) / out_tokens, 3) if out_tokens else None,
        "ttft_p50_ms": pct(ttft, 50), "ttft_p90_ms": pct(ttft, 90), "ttft_p95_ms": pct(ttft, 95),
        "e2e_p50_ms": pct(e2e, 50), "e2e_p90_ms": pct(e2e, 90), "e2e_p95_ms": pct(e2e, 95),
        "output_tokens_mean": round(statistics.mean(r["completion_tokens"] for r in ok), 1) if ok else None,
        "cost_usd_sum": round(sum(r["cost_usd"] or 0 for r in ok), 6),
        "served_families": sorted({r["served_model_family"] for r in ok if r["served_model_family"]}),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Fixed-duration, continuously refilled sustained load on top of harness.run_one.")
    p.add_argument("--dataset", required=True)
    p.add_argument("--arms", required=True)
    p.add_argument("--levels", default="4,8,16")
    p.add_argument("--duration", type=float, default=90.0, help="seconds per level including ramp")
    p.add_argument("--ramp", type=float, default=15.0, help="seconds at the start of each level not counted")
    p.add_argument("--settle-seconds", type=float, default=15.0, dest="settle_seconds")
    p.add_argument("--region", required=True)
    p.add_argument("--client-location", default="unknown", dest="client_location")
    p.add_argument("--deployment-type", default="paygo", dest="deployment_type")
    p.add_argument("--max-output-tokens", type=int, default=512, dest="max_output_tokens")
    p.add_argument("--pricing", default=str(harness.DEFAULT_PRICING))
    p.add_argument("--models", default=str(harness.DEFAULT_MODELS))
    args = p.parse_args()
    if args.ramp >= args.duration:
        raise SystemExit("--ramp must be shorter than --duration")

    dataset = harness.load_jsonl(Path(args.dataset))
    pricing = harness.load_pricing(Path(args.pricing))
    registry = harness.load_model_registry(Path(args.models))
    arms = [parse_arm(s.strip()) for s in args.arms.split(",") if s.strip()]
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    client, endpoint = harness.build_client()
    endpoint_host = urlparse(endpoint).netloc or endpoint
    rtt = harness.measure_rtt(endpoint_host)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"sustained_{stamp}.jsonl"
    summary_path = OUTPUT_DIR / f"sustained_{stamp}.summary.json"
    print(f"region={args.region} endpoint={endpoint_host} client={args.client_location} rtt={rtt}ms")
    print(f"arms={[a['arm_id'] for a in arms]} levels={levels} duration={args.duration}s ramp={args.ramp}s")
    print("tools: NONE; sdk retries: 0; continuous refill; counting only requests started after ramp and finished before deadline")
    print(f"writing {out_path}\n")

    lock = threading.Lock()
    summaries = []
    with out_path.open("w", encoding="utf-8") as fh:
        for arm in arms:
            print(f"== {arm['arm_id']} ({arm['api']})")
            for level in levels:
                records, ramp_s, deadline_s = run_level(client, arm, dataset, level, args.duration, args.ramp, args, fh, lock,
                                                        stamp, endpoint_host, rtt, pricing, registry)
                s = summarize(arm, level, records, ramp_s, deadline_s, args.duration)
                summaries.append(s)
                print(f"  N={level:>2}: counted {s['counted']:>4} ok {s['ok']:>4} 429={s['errors_429']:<3} other={s['other_errors']:<2} "
                      f"req/s={s['requests_per_s']:<6} out tok/s={s['aggregate_output_tok_per_s']:<7} "
                      f"TTFT p50/p90={s['ttft_p50_ms']}/{s['ttft_p90_ms']} E2E p50/p90={s['e2e_p50_ms']}/{s['e2e_p90_ms']}")
                summary_path.write_text(json.dumps({"run_id": stamp, "region": args.region, "endpoint_host": endpoint_host,
                                                    "client_location": args.client_location, "network_rtt_ms": rtt,
                                                    "levels": levels, "duration_s": args.duration, "ramp_s": args.ramp,
                                                    "dataset": args.dataset, "summaries": summaries}, indent=2), encoding="utf-8")
                time.sleep(args.settle_seconds)
    print(f"\nDone. records → {out_path}\nsummary → {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
