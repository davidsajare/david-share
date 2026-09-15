"""Validate the paired router-overhead run and regenerate its CSV artefacts.

Runs offline. The raw JSONL is pinned by SHA-256; the builder refuses any other
file. Every pair is one prompt measured on the router and on the direct
deployment back to back, so the per-pair difference is the router's own
contribution to client-observed latency under otherwise identical conditions.

Usage:
    python scripts/build_paired_overhead.py
    python scripts/build_paired_overhead.py --check

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import analyze  # noqa: E402

OUTPUT = ROOT / "outputs"
RUN = "20260915_162607"
RAW_SHA256 = "94a35464513385f71b0bd1e1dccf99d0a1565674ed34dea2a71c8b2e159e2e04"
ROUTER_ARM = "router-sol-luna-cost"
DIRECT_ARM = "gpt-5.6-luna"
BILLING_MODEL = "gpt-5.6-luna"
BURST_MS = 50
ENDPOINT_PLACEHOLDER = "YOUR-ENDPOINT.cognitiveservices.azure.com"


def load_records() -> tuple[list[dict], dict]:
    raw = OUTPUT / f"router_overhead_paired_{RUN}.jsonl"
    payload = raw.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest != RAW_SHA256:
        raise ValueError(f"raw file does not match its pinned checksum: {digest}")
    records = [json.loads(line) for line in payload.decode("utf-8").splitlines() if line.strip()]
    provenance = json.loads((OUTPUT / f"provenance_paired_{RUN}.json").read_text(encoding="utf-8"))
    return records, provenance


def validate(records: list[dict], provenance: dict) -> None:
    if provenance["vm_metadata"]["location"].casefold() != "swedencentral":
        raise ValueError("IMDS does not confirm the benchmark VM region")
    expected = provenance["prompts"] * (provenance["iterations"] + 1) * 2
    if len(records) != expected:
        raise ValueError(f"expected {expected} records, found {len(records)}")
    conditions = {(r["region"], r["endpoint_host"], r["api"], r["max_output_tokens"],
                   r["reasoning_effort"], r["tools_enabled"]) for r in records}
    if len(conditions) != 1:
        raise ValueError("mixed test conditions inside the paired run")
    if records[0]["endpoint_host"] != ENDPOINT_PLACEHOLDER:
        raise ValueError("committed copy must carry the endpoint placeholder")
    for r in records:
        if r["status"] != "completed" or r["error"] or r["truncated"]:
            raise ValueError(f"invalid measurement: {r['arm']}/{r['question_id']}/{r['pair_index']}")
        if r["ttft_ms"] is None or r["ttft_ms"] > r["e2e_ms"]:
            raise ValueError("inconsistent timing record")
        if r["warmup"] != (r["pair_index"] == 0):
            raise ValueError("incorrect warm-up flag")
    pairs = defaultdict(dict)
    for r in records:
        pairs[(r["question_id"], r["pair_index"])][r["side"]] = r
    for key, sides in pairs.items():
        if set(sides) != {"router", "direct"}:
            raise ValueError(f"incomplete pair {key}")
        if {sides["router"]["position_in_pair"], sides["direct"]["position_in_pair"]} != {1, 2}:
            raise ValueError(f"pair {key} is not one request per side")


def served_family(record: dict) -> str:
    return "luna" if "luna" in (record.get("model_actually_served") or "") else "other"


def build_pairs(records: list[dict]) -> list[dict]:
    grouped = defaultdict(dict)
    for r in records:
        if not r["warmup"]:
            grouped[(r["question_id"], r["pair_index"])][r["side"]] = r
    rows = []
    for (qid, pair_index), sides in sorted(grouped.items()):
        router, direct = sides["router"], sides["direct"]
        rows.append({
            "question_id": qid, "tier": router["tier"], "category": router["category"],
            "pair_index": pair_index, "router_first": router["router_first"],
            "router_served": router["model_actually_served"],
            "direct_served": direct["model_actually_served"],
            "same_served_model": served_family(router) == served_family(direct) == "luna",
            "router_ttft_ms": router["ttft_ms"], "direct_ttft_ms": direct["ttft_ms"],
            "ttft_delta_ms": round(router["ttft_ms"] - direct["ttft_ms"], 1),
            "router_e2e_ms": router["e2e_ms"], "direct_e2e_ms": direct["e2e_ms"],
            "e2e_delta_ms": round(router["e2e_ms"] - direct["e2e_ms"], 1),
            "router_self_reported_ms": (router.get("routing") or {}).get("router_latency_ms"),
            "router_output_tokens": router["completion_tokens"],
            "direct_output_tokens": direct["completion_tokens"],
        })
    return rows


def summarize(records: list[dict], pairs: list[dict], pricing: dict) -> list[dict]:
    measured = [r for r in records if not r["warmup"]]
    rows = []
    for arm in (ROUTER_ARM, DIRECT_ARM):
        group = [r for r in measured if r["arm"] == arm]
        costs = [analyze.cost_for(pricing, BILLING_MODEL, r["prompt_tokens"], r["cached_tokens"],
                                  r["completion_tokens"]) for r in group]
        self_reported = [(r.get("routing") or {}).get("router_latency_ms") for r in group]
        self_reported = [v for v in self_reported if v is not None]
        rows.append({
            "row": "arm", "arm": arm, "n": len(group),
            "served_luna_share": sum(served_family(r) == "luna" for r in group) / len(group),
            "ttft_p50_ms": analyze.pct([r["ttft_ms"] for r in group], 50),
            "ttft_p90_ms": analyze.pct([r["ttft_ms"] for r in group], 90),
            "ttft_p95_ms": analyze.pct([r["ttft_ms"] for r in group], 95),
            "e2e_p50_ms": analyze.pct([r["e2e_ms"] for r in group], 50),
            "e2e_p90_ms": analyze.pct([r["e2e_ms"] for r in group], 90),
            "burst_lt50ms_share": sum(r["decode_ms"] is not None and r["decode_ms"] < BURST_MS
                                      for r in group) / len(group),
            "output_tokens_mean": statistics.mean(r["completion_tokens"] for r in group),
            "reasoning_tokens_mean": statistics.mean(r["reasoning_tokens"] for r in group),
            "cost_usd_per_1000_requests": statistics.mean(costs) * 1000,
            "router_self_reported_p50_ms": analyze.pct(self_reported, 50) if self_reported else None,
            "router_self_reported_p95_ms": analyze.pct(self_reported, 95) if self_reported else None,
        })
    usable = [p for p in pairs if p["same_served_model"]]
    for label, subset in (("all", usable),
                          ("router_first", [p for p in usable if p["router_first"]]),
                          ("direct_first", [p for p in usable if not p["router_first"]]),
                          ("simple", [p for p in usable if p["tier"] == "simple"]),
                          ("moderate", [p for p in usable if p["tier"] == "moderate"]),
                          ("complex", [p for p in usable if p["tier"] == "complex"])):
        deltas = [p["ttft_delta_ms"] for p in subset]
        e2e = [p["e2e_delta_ms"] for p in subset]
        rows.append({
            "row": "paired_delta", "subset": label, "pairs": len(subset),
            "ttft_delta_p25_ms": analyze.pct(deltas, 25), "ttft_delta_p50_ms": analyze.pct(deltas, 50),
            "ttft_delta_p75_ms": analyze.pct(deltas, 75), "ttft_delta_p90_ms": analyze.pct(deltas, 90),
            "ttft_delta_mean_ms": statistics.mean(deltas),
            "router_slower_share_ttft": sum(d > 0 for d in deltas) / len(deltas),
            "e2e_delta_p25_ms": analyze.pct(e2e, 25), "e2e_delta_p50_ms": analyze.pct(e2e, 50),
            "e2e_delta_p75_ms": analyze.pct(e2e, 75), "e2e_delta_p90_ms": analyze.pct(e2e, 90),
            "e2e_delta_mean_ms": statistics.mean(e2e),
            "router_slower_share_e2e": sum(d > 0 for d in e2e) / len(e2e),
        })
    rows.append({"row": "pairs_excluded_for_different_served_model",
                 "pairs": len(pairs) - len(usable)})
    return rows


def render_csv(rows: list[dict]) -> str:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, lineterminator="\r\n")
    writer.writeheader()
    writer.writerows(rows)
    return "\ufeff" + buffer.getvalue()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if any artefact would change")
    arguments = parser.parse_args()

    records, provenance = load_records()
    validate(records, provenance)
    pricing = analyze.load_pricing(ROOT / "config" / "pricing.json")
    analyze._REGISTRY = analyze.load_registry(ROOT / "config" / "models.json")
    pairs = build_pairs(records)
    summary = summarize(records, pairs, pricing)

    artefacts = {
        OUTPUT / f"router_overhead_paired_{RUN}.csv": render_csv(pairs),
        OUTPUT / "router_overhead_paired_summary.csv": render_csv(summary),
    }
    stale = [p.name for p, text in artefacts.items()
             if not p.exists() or p.read_bytes() != text.encode("utf-8")]
    if arguments.check:
        if stale:
            raise SystemExit("regenerate and commit: " + ", ".join(stale))
    else:
        for path, text in artefacts.items():
            path.write_text(text, encoding="utf-8", newline="")
    usable = sum(1 for p in pairs if p["same_served_model"])
    print(f"VERIFIED: {len(records)} records, {len(pairs)} measured pairs, {usable} same-model pairs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
