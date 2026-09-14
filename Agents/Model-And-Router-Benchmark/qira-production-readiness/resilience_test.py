#!/usr/bin/env python3
"""
Rate-limit and fallback resilience test.

Drives a deliberately small deployment past its PAYGO rate limit at fixed
concurrency for a fixed duration and measures, for each policy, what the user
would experience:

  none       primary only — every 429 is a failed request (what the router/model
             does on its own when the client has no fallback)
  reactive   primary, then fallback on 429/5xx/timeout at request time
  proactive  as reactive, plus routing straight to the fallback while the primary's
             rate-limit headers show utilization ≥ threshold

Same scheduling as sustained_load.py (continuous refill, ramp excluded), same
measurement (harness.run_one). The fallback decision for every request is
attached to its record so the report can show which backend served it, how many
attempts it took, the Retry-After the primary asked for, and how much latency the
failed attempt(s) added.

Usage (same-region VM):
    python resilience_test.py --dataset datasets/qira_scenarios.jsonl \
        --primary gpt-5.6-luna-lowcap --fallback gpt-5.6-luna --effort none \
        --policies none,reactive,proactive --level 8 --duration 75 --ramp 15 \
        --region swedencentral --client-location swedencentral-linux-vm

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
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import fallback_client
import harness
from sustained_load import classify_error, pct

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs"


def run_policy(fc, arm_id, chain, policy, dataset, level, duration, ramp, args, fh, lock, stamp, endpoint_host, rtt, pricing, registry):
    t0 = time.perf_counter()
    deadline, ramp_end = t0 + duration, t0 + ramp
    records, next_index, index_lock = [], [0], threading.Lock()

    def worker(worker_id):
        while time.perf_counter() < deadline:
            with index_lock:
                i = next_index[0]
                next_index[0] += 1
            item = dataset[i % len(dataset)]
            cap = int(item.get("max_output_tokens", args.max_output_tokens)) + harness.REASONING_HEADROOM_UNIFORM
            started = time.perf_counter()
            m = harness.run_one(fc, "fallback-chain", item, cap, args.effort, api=args.api)
            finished = time.perf_counter()
            d = fallback_client.last_decision()
            served = m["model_actually_served"]
            billing = harness.billing_model_name(served)
            counted = started >= ramp_end and finished <= deadline
            record = {
                "run_id": stamp, "kind": "resilience", "arm": arm_id, "policy": policy, "chain": chain,
                "reasoning_effort": args.effort, "api": args.api, "level": level, "worker": worker_id, "sequence": i,
                "question_id": item["id"], "scenario": item.get("scenario"),
                "region": args.region, "endpoint_host": endpoint_host, "deployment_type": args.deployment_type,
                "client_location": args.client_location, "network_rtt_ms": rtt, "tools_enabled": False, "warmup": False,
                "started_offset_ms": round((started - t0) * 1000, 1), "finished_offset_ms": round((finished - t0) * 1000, 1),
                "in_ramp": started < ramp_end, "cut_by_deadline": finished > deadline, "counted": counted,
                "served_by_deployment": d.served_by if d else None,
                "served_by_fallback": bool(d and d.served_by and d.served_by != chain[0]),
                "attempts": d.attempts if d else None, "attempt_count": len(d.attempts) if d else None,
                "in_stream_fallbacks": d.in_stream_fallbacks if d else None,
                "primary_rate_limited": bool(d and any(a.get("position") == 0 and a.get("rate_limit") for a in d.attempts)),
                "primary_failure_phase": next((a.get("phase") for a in (d.attempts if d else []) if a.get("position") == 0 and a.get("phase")), None),
                "proactive_skip": d.proactive_skip if d else None, "utilization_before": d.utilization_before if d else None,
                "primary_retry_after_s": next((a.get("retry_after_s") for a in (d.attempts if d else []) if a.get("position") == 0 and a.get("retry_after_s") is not None), None),
                "fallback_overhead_ms": d.fallback_overhead_ms if d else None, "chain_exhausted": d.exhausted if d else None,
                "model_actually_served": served, "served_model_family": billing,
                "error_class": classify_error(m["error"]),
                "cost_usd": harness.compute_cost(pricing, billing or chain[0], m["prompt_tokens"], m["cached_tokens"], m["completion_tokens"], registry),
                "response_chars": len(m["response_text"]),
                "response_sha256": hashlib.sha256(m["response_text"].encode()).hexdigest(),
                **{k: m[k] for k in ("ttft_ms", "e2e_ms", "decode_ms", "tpot_ms", "decode_tps", "prompt_tokens", "cached_tokens",
                                     "completion_tokens", "reasoning_tokens", "status", "truncated", "incomplete_reason", "error")},
                "finish_reason": m.get("finish_reason"),
                "response_text": m["response_text"],
            }
            with lock:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                fh.flush()
                records.append(record)
            if m["error"] and args.failure_pause > 0:
                # A client without a fallback would not hammer a throttled deployment at
                # full speed; APIM's retry policy also waits interval="1" between attempts.
                time.sleep(args.failure_pause)

    threads = [threading.Thread(target=worker, args=(w,), daemon=True) for w in range(level)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return records, ramp, duration


def summarize(policy, chain, level, records, ramp, duration):
    counted = [r for r in records if r["counted"]]
    ok = [r for r in counted if not r["error"] and r["status"] == "completed" and not r["truncated"]]
    errors = [r for r in counted if r["error"]]
    window = duration - ramp
    by_fb = [r for r in ok if r["served_by_fallback"]]
    by_primary = [r for r in ok if not r["served_by_fallback"]]
    retry_after = [r["primary_retry_after_s"] for r in counted if r["primary_retry_after_s"] is not None]
    return {
        "policy": policy, "chain": chain, "level": level, "window_s": window, "all_records": len(records), "counted": len(counted),
        "ok": len(ok), "errors": len(errors), "errors_429": sum(1 for r in errors if r["error_class"] == "429"),
        "other_errors": sum(1 for r in errors if r["error_class"] != "429"),
        "success_rate": round(len(ok) / len(counted), 4) if counted else None,
        "served_by_primary": len(by_primary), "served_by_fallback": len(by_fb),
        "fallback_share_of_ok": round(len(by_fb) / len(ok), 4) if ok else None,
        "proactive_skips": sum(1 for r in counted if r["proactive_skip"]),
        "primary_rate_limited": sum(1 for r in counted if r["primary_rate_limited"]),
        "primary_failure_phases": dict(Counter(r["primary_failure_phase"] for r in counted if r["primary_failure_phase"])),
        "in_stream_fallbacks": sum(r["in_stream_fallbacks"] or 0 for r in counted),
        "primary_429_seen": sum(1 for r in counted for a in (r["attempts"] or []) if a.get("position") == 0 and a.get("status") == 429),
        "retry_after_s_p50": pct(retry_after, 50), "retry_after_s_max": max(retry_after) if retry_after else None,
        "requests_per_s": round(len(ok) / window, 3),
        "aggregate_output_tok_per_s": round(sum(r["completion_tokens"] for r in ok) / window, 1),
        "ttft_p50_ms": pct([r["ttft_ms"] for r in ok if r["ttft_ms"] is not None], 50),
        "ttft_p90_ms": pct([r["ttft_ms"] for r in ok if r["ttft_ms"] is not None], 90),
        "ttft_primary_p50_ms": pct([r["ttft_ms"] for r in by_primary if r["ttft_ms"] is not None], 50),
        "ttft_fallback_p50_ms": pct([r["ttft_ms"] for r in by_fb if r["ttft_ms"] is not None], 50),
        "fallback_overhead_ms_p50": pct([r["fallback_overhead_ms"] for r in by_fb if r["fallback_overhead_ms"]], 50),
        "fallback_overhead_ms_p90": pct([r["fallback_overhead_ms"] for r in by_fb if r["fallback_overhead_ms"]], 90),
        "e2e_p50_ms": pct([r["e2e_ms"] for r in ok], 50), "e2e_p90_ms": pct([r["e2e_ms"] for r in ok], 90),
        "cost_usd_sum": round(sum(r["cost_usd"] or 0 for r in ok), 6),
        "served_families": sorted({r["served_model_family"] for r in ok if r["served_model_family"]}),
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Drive a small deployment past its rate limit under three fallback policies.")
    p.add_argument("--dataset", required=True)
    p.add_argument("--primary", required=True)
    p.add_argument("--fallback", required=True, help="comma-separated fallback deployments, tried in order")
    p.add_argument("--effort", default=None, help="reasoning_effort sent to every backend ('-' = not sent)")
    p.add_argument("--api", choices=["responses", "chat"], default="responses")
    p.add_argument("--policies", default="none,reactive,proactive")
    p.add_argument("--threshold", type=float, default=0.8)
    p.add_argument("--level", type=int, default=8)
    p.add_argument("--duration", type=float, default=75.0)
    p.add_argument("--ramp", type=float, default=15.0)
    p.add_argument("--settle-seconds", type=float, default=70.0, dest="settle_seconds",
                   help="pause between policies so the primary's per-minute window resets")
    p.add_argument("--failure-pause", type=float, default=1.0, dest="failure_pause",
                   help="seconds a worker waits after a failed request before its next one")
    p.add_argument("--region", required=True)
    p.add_argument("--client-location", default="unknown", dest="client_location")
    p.add_argument("--deployment-type", default="paygo", dest="deployment_type")
    p.add_argument("--max-output-tokens", type=int, default=512, dest="max_output_tokens")
    p.add_argument("--pricing", default=str(harness.DEFAULT_PRICING))
    p.add_argument("--models", default=str(harness.DEFAULT_MODELS))
    args = p.parse_args()
    if args.effort in ("-", ""):
        args.effort = None

    dataset = harness.load_jsonl(Path(args.dataset))
    pricing = harness.load_pricing(Path(args.pricing))
    registry = harness.load_model_registry(Path(args.models))
    chain = [args.primary, *[f.strip() for f in args.fallback.split(",") if f.strip()]]
    policies = [x.strip() for x in args.policies.split(",") if x.strip()]

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"resilience_{stamp}.jsonl"
    summary_path = OUTPUT_DIR / f"resilience_{stamp}.summary.json"
    lock = threading.Lock()
    summaries = []
    endpoint_host = rtt = None
    with out_path.open("w", encoding="utf-8") as fh:
        for policy in policies:
            fc, endpoint = fallback_client.build_fallback_client(chain, policy, args.threshold)
            endpoint_host = urlparse(endpoint).netloc or endpoint
            if rtt is None:
                rtt = harness.measure_rtt(endpoint_host)
                print(f"region={args.region} endpoint={endpoint_host} client={args.client_location} rtt={rtt}ms")
                print(f"chain={chain} policies={policies} level={args.level} duration={args.duration}s ramp={args.ramp}s api={args.api}")
                print("tools: NONE; sdk retries: 0; Retry-After recorded, never slept on\n")
            arm_id = f"{chain[0]}→{'/'.join(chain[1:])}[{policy}]"
            print(f"== policy={policy}")
            records, ramp, duration = run_policy(fc, arm_id, chain, policy, dataset, args.level, args.duration, args.ramp, args,
                                                 fh, lock, stamp, endpoint_host, rtt, pricing, registry)
            s = summarize(policy, chain, args.level, records, ramp, duration)
            summaries.append(s)
            print(f"  counted {s['counted']} ok {s['ok']} ({s['success_rate']}) rate-limit-failed={s['errors_429']} primary-rate-limited={s['primary_rate_limited']} phases={s['primary_failure_phases']} "
                  f"by-fallback={s['served_by_fallback']} in-stream-fallbacks={s['in_stream_fallbacks']} proactive-skips={s['proactive_skips']} retry-after p50={s['retry_after_s_p50']}s "
                  f"TTFT p50 primary/fallback={s['ttft_primary_p50_ms']}/{s['ttft_fallback_p50_ms']}ms overhead p50={s['fallback_overhead_ms_p50']}ms")
            summary_path.write_text(json.dumps({"run_id": stamp, "region": args.region, "endpoint_host": endpoint_host,
                                                "client_location": args.client_location, "network_rtt_ms": rtt, "chain": chain,
                                                "level": args.level, "duration_s": args.duration, "ramp_s": args.ramp,
                                                "threshold": args.threshold, "api": args.api, "effort": args.effort,
                                                "failure_pause_s": args.failure_pause,
                                                "dataset": args.dataset, "summaries": summaries}, indent=2), encoding="utf-8")
            if policy != policies[-1]:
                time.sleep(args.settle_seconds)
    print(f"\nDone. records → {out_path}\nsummary → {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
