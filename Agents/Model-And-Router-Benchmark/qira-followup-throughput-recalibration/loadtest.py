#!/usr/bin/env python3
"""
Stepped-concurrency load test for the Qira candidate deployments.

The single-request matrix (harness.py, concurrency 1) answers "how fast is one
turn". This script answers the question it cannot: what happens to TTFT / E2E
and to aggregate tokens-per-second when 4, 8, 16 requests are in flight at once
against a PAYGO deployment, and whether the deployment starts returning 429.

Measurement is delegated to harness.run_one, so TTFT, TPOT and usage are
defined exactly as in the single-request runs; only the scheduling differs.
Each level submits --requests-per-level requests to a thread pool of <level>
workers, cycling through the dataset prompts, and records wall-clock start/end
per request. Levels run in ascending order, one arm at a time.

Usage (on the same-region VM):
    python loadtest.py --dataset datasets/qira_scenarios.jsonl \
        --arms gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat \
        --levels 1,4,8,16 --requests-per-level 48 --region swedencentral \
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
from concurrent.futures import ThreadPoolExecutor, as_completed
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
    """'deployment[@effort][#api]' -> arm dict. effort '-' or absent = not sent."""
    api = "responses"
    if "#" in spec:
        spec, api = spec.split("#", 1)
    deployment, effort = (spec.split("@", 1) + [None])[:2]
    if effort in ("-", ""):
        effort = None
    if api not in ("responses", "chat"):
        raise SystemExit(f"Unknown api surface {api!r} in {spec}")
    if deployment.startswith("router-") and api != "chat":
        api = "chat"  # router deployments only expose the routing trace on Chat Completions
    return {"deployment": deployment, "effort": effort, "api": api,
            "arm_id": f"{deployment}@{effort}" if effort else deployment}


def classify_error(error: str | None) -> str | None:
    if not error:
        return None
    if "RateLimitError" in error or " 429" in error:
        return "429"
    if "APITimeoutError" in error or "Timeout" in error:
        return "timeout"
    if "APIConnectionError" in error:
        return "connection"
    return "other"


def main() -> int:
    p = argparse.ArgumentParser(description="Stepped-concurrency load test built on harness.run_one.")
    p.add_argument("--dataset", required=True)
    p.add_argument("--arms", required=True, help="comma-separated deployment[@effort][#api]")
    p.add_argument("--levels", default="1,4,8,16")
    p.add_argument("--requests-per-level", type=int, default=48, dest="requests_per_level")
    p.add_argument("--region", required=True)
    p.add_argument("--client-location", default="unknown", dest="client_location")
    p.add_argument("--deployment-type", default="paygo", dest="deployment_type")
    p.add_argument("--settle-seconds", type=float, default=10.0, dest="settle_seconds",
                   help="pause between levels so rate-limit windows do not bleed across levels")
    p.add_argument("--max-output-tokens", type=int, default=512, dest="max_output_tokens")
    p.add_argument("--pricing", default=str(harness.DEFAULT_PRICING))
    p.add_argument("--models", default=str(harness.DEFAULT_MODELS))
    args = p.parse_args()

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
    out_path = OUTPUT_DIR / f"loadtest_{stamp}.jsonl"
    summary_path = OUTPUT_DIR / f"loadtest_{stamp}.summary.json"

    print(f"region={args.region} endpoint={endpoint_host} client={args.client_location} rtt={rtt}ms")
    print(f"arms={[a['arm_id'] for a in arms]} levels={levels} requests/level={args.requests_per_level}")
    print("tools: NONE; sdk retries: 0 (every 429/5xx is recorded, never silently retried)")
    print(f"writing {out_path}\n")

    lock = threading.Lock()
    summaries = []

    def one(arm, item, level, slot, level_t0, fh, is_warmup=False):
        cap = int(item.get("max_output_tokens", args.max_output_tokens)) + harness.REASONING_HEADROOM_UNIFORM
        started = time.perf_counter()
        m = harness.run_one(client, arm["deployment"], item, cap, arm["effort"], api=arm["api"])
        finished = time.perf_counter()
        served = m["model_actually_served"]
        billing = harness.billing_model_name(served)
        routing = m.get("routing") or {}
        record = {
            "run_id": stamp, "kind": "loadtest", "arm": arm["arm_id"], "model_requested": arm["deployment"],
            "reasoning_effort": arm["effort"], "api": arm["api"], "level": level, "slot": slot, "warmup": is_warmup,
            "question_id": item["id"], "scenario": item.get("scenario"),
            "region": args.region, "endpoint_host": endpoint_host, "deployment_type": args.deployment_type,
            "client_location": args.client_location, "network_rtt_ms": rtt, "tools_enabled": False,
            "started_offset_ms": round((started - level_t0) * 1000, 1),
            "finished_offset_ms": round((finished - level_t0) * 1000, 1),
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
            # Synthetic benchmark answers are retained for report reproduction.
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")  # lgtm[py/clear-text-storage-sensitive-data]
            fh.flush()
        return record

    with out_path.open("w", encoding="utf-8") as fh:
        for arm in arms:
            print(f"== {arm['arm_id']} ({arm['api']})")
            one(arm, dataset[0], 0, 0, time.perf_counter(), fh, is_warmup=True)
            for level in levels:
                items = [dataset[i % len(dataset)] for i in range(args.requests_per_level)]
                level_t0 = time.perf_counter()
                records = []
                with ThreadPoolExecutor(max_workers=level) as pool:
                    futures = [pool.submit(one, arm, item, level, slot, level_t0, fh) for slot, item in enumerate(items)]
                    for f in as_completed(futures):
                        records.append(f.result())
                wall = time.perf_counter() - level_t0
                ok = [r for r in records if not r["error"] and r["status"] == "completed" and not r["truncated"]]
                errors = [r for r in records if r["error"]]
                ttft = [r["ttft_ms"] for r in ok if r["ttft_ms"] is not None]
                e2e = [r["e2e_ms"] for r in ok]
                tps = [r["decode_tps"] for r in ok if r["decode_tps"] is not None]
                out_tokens = sum(r["completion_tokens"] for r in ok)
                summary = {
                    "arm": arm["arm_id"], "api": arm["api"], "level": level, "requests": len(records), "ok": len(ok),
                    "errors": len(errors), "errors_429": sum(1 for r in errors if r["error_class"] == "429"),
                    "truncated": sum(1 for r in records if r["truncated"]),
                    "wall_s": round(wall, 2), "requests_per_s": round(len(ok) / wall, 3) if wall else None,
                    "aggregate_output_tok_per_s": round(out_tokens / wall, 1) if wall else None,
                    "aggregate_visible_tok_per_s": round(sum(r["completion_tokens"] - r["reasoning_tokens"] for r in ok) / wall, 1) if wall else None,
                    "ttft_p50_ms": pct(ttft, 50), "ttft_p90_ms": pct(ttft, 90), "ttft_p95_ms": pct(ttft, 95),
                    "e2e_p50_ms": pct(e2e, 50), "e2e_p90_ms": pct(e2e, 90), "e2e_p95_ms": pct(e2e, 95),
                    "per_request_tok_per_s_p50": pct(tps, 50),
                    "output_tokens_mean": round(statistics.mean(r["completion_tokens"] for r in ok), 1) if ok else None,
                    "cost_usd_sum": round(sum(r["cost_usd"] or 0 for r in ok), 6),
                    "served_families": sorted({r["served_model_family"] for r in ok if r["served_model_family"]}),
                }
                summaries.append(summary)
                print(f"  level {level:>2}: ok {len(ok):>3}/{len(records):<3} 429={summary['errors_429']:<2} "
                      f"wall={wall:6.1f}s req/s={summary['requests_per_s']:<6} agg out tok/s={summary['aggregate_output_tok_per_s']:<8} "
                      f"TTFT p50/p90={summary['ttft_p50_ms']}/{summary['ttft_p90_ms']}ms E2E p50/p90={summary['e2e_p50_ms']}/{summary['e2e_p90_ms']}ms")
                summary_path.write_text(json.dumps({"run_id": stamp, "region": args.region, "endpoint_host": endpoint_host,
                                                    "client_location": args.client_location, "network_rtt_ms": rtt,
                                                    "levels": levels, "requests_per_level": args.requests_per_level,
                                                    "dataset": args.dataset, "summaries": summaries}, indent=2), encoding="utf-8")
                if level != levels[-1]:
                    time.sleep(args.settle_seconds)
            time.sleep(args.settle_seconds)

    print(f"\nDone. records → {out_path}\nsummary → {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
