"""Paired router-overhead measurement: router versus direct, same everything.

Answers one question the router study could not: on identical conditions, does
putting Model Router in front of a model add client-observed latency, and how
much? The original comparison was confounded three ways - the router arms ran
on GlobalStandard while the direct baselines ran on DataZoneStandard, the arms
ran serially hours apart, and most direct answers arrived in one burst. This
run removes all three:

  same SKU        router-sol-luna-cost (GlobalStandard) vs gpt-5.6-luna (GlobalStandard)
  same API        Chat Completions for both, the only surface the router exposes
  same model      cost mode routed every request to Luna in the study, so the
                  served model is the same on both sides; pairs where it is not
                  are kept in the raw file and excluded from the paired statistic
  same effort     reasoning_effort is not sent on either side
  same moment     each prompt is measured as router/direct pairs back to back,
                  alternating which side goes first, so time-of-run drift and
                  ordering bias cancel across pairs

Every request is one streamed call through the study harness's run_one, with
no tools and no web search, from the same-region VM.

Usage (on the runner VM):
    python paired_overhead.py --out /var/lib/benchmark/paired --iterations 3

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener

HERE = Path(__file__).resolve()
_override = os.environ.get("MRB_PROJECT_ROOT")
PROJECT = (Path(_override).resolve() if _override
           else next(p for p in HERE.parents if (p / "production-readiness" / "harness.py").is_file()))
HARNESS_PATH = PROJECT / "production-readiness" / "harness.py"
DATASET = PROJECT / "model-router-validation" / "datasets" / "router_taskb.jsonl"

ROUTER_ARM = "router-sol-luna-cost"
DIRECT_ARM = "gpt-5.6-luna"
# The router study sent 512 + 8192 for every router prompt; identical here.
ANSWER_BUDGET = 512
REASONING_HEADROOM = 8192
MAX_OUTPUT_TOKENS = ANSWER_BUDGET + REASONING_HEADROOM
SLEEP_BETWEEN_REQUESTS_S = 0.5


def load_harness():
    spec = importlib.util.spec_from_file_location("study_harness", HARNESS_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_prompts() -> list[dict]:
    rows = [json.loads(line) for line in DATASET.read_text(encoding="utf-8").splitlines() if line.strip()]
    usable = [r for r in rows if "withheld" not in r["text"].lower()]
    return usable


def imds() -> dict:
    request = Request(
        "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01",
        headers={"Metadata": "true"},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
        data = json.load(response)
    return {k: data.get(k) for k in ("name", "location", "vmSize", "osType")}


def measure(harness, client, arm: str, item: dict) -> dict:
    started = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    result = harness.run_one(client, arm, item, MAX_OUTPUT_TOKENS, None, api="chat")
    text = result.pop("response_text", "") or ""
    result["response_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    result["response_chars"] = len(text)
    result["started_at_utc"] = started
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, help="directory for the JSONL and summary")
    parser.add_argument("--iterations", type=int, default=3, help="measured pairs per prompt")
    parser.add_argument("--limit", type=int, default=0, help="debug: only the first N prompts")
    parser.add_argument("--resume", default="", help="existing JSONL of this design to continue; prompts "
                        "that already have every pair recorded are skipped and new records are appended")
    args = parser.parse_args()

    harness = load_harness()
    client, endpoint = harness.build_client()
    endpoint_host = urlparse(endpoint).netloc or endpoint
    api_version = getattr(client, "_api_version", None) or os.environ.get("AZURE_OPENAI_API_VERSION")
    prompts = load_prompts()
    if args.limit:
        prompts = prompts[: args.limit]
    per_prompt = (args.iterations + 1) * 2
    completed_prompts: set[str] = set()
    resume_windows: list[dict] = []
    if args.resume:
        resume_path = Path(args.resume)
        existing = [json.loads(line) for line in resume_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        counts: dict[str, int] = {}
        for record in existing:
            counts[record["question_id"]] = counts.get(record["question_id"], 0) + 1
        completed_prompts = {qid for qid, n in counts.items() if n == per_prompt}
        partial = {qid for qid, n in counts.items() if n != per_prompt}
        kept = [r for r in existing if r["question_id"] in completed_prompts]
        resume_path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept), encoding="utf-8")
        run_id = existing[0]["run_id"]
        resume_windows.append({"records_kept": len(kept), "prompts_complete": len(completed_prompts),
                               "partial_prompts_discarded": sorted(partial),
                               "first_window_last_record_utc": max(r["started_at_utc"] for r in kept),
                               "first_window_script_sha256": os.environ.get("MRB_FIRST_WINDOW_SCRIPT_SHA256"),
                               "reason": os.environ.get("MRB_RESUME_REASON")})
        raw_path = resume_path
        print(f"resuming {run_id}: {len(completed_prompts)} prompts complete, "
              f"{len(partial)} partial prompt(s) discarded and re-measured: {sorted(partial)}", flush=True)
    else:
        run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        raw_path = Path(args.out) / f"router_overhead_paired_{run_id}.jsonl"
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    vm = imds()
    provenance = {
        "run_id": run_id,
        "design": "paired ABAB, alternating first side per pair, 1 warm-up pair + measured pairs per prompt",
        "router_arm": ROUTER_ARM,
        "direct_arm": DIRECT_ARM,
        "api": "chat",
        "api_version": api_version,
        "reasoning_effort": None,
        "max_output_tokens": MAX_OUTPUT_TOKENS,
        "answer_budget": ANSWER_BUDGET,
        "reasoning_headroom": REASONING_HEADROOM,
        "prompts": len(prompts),
        "iterations": args.iterations,
        "sleep_between_requests_s": SLEEP_BETWEEN_REQUESTS_S,
        "endpoint_host": endpoint_host,
        "region": "swedencentral",
        "client_location": "swedencentral-linux-vm",
        "vm_metadata": vm,
        "python": platform.python_version(),
        "openai": importlib.metadata.version("openai"),
        "azure_identity": importlib.metadata.version("azure-identity"),
        "source_sha256": {
            "scripts/paired_overhead.py": hashlib.sha256(HERE.read_bytes()).hexdigest(),
            "production-readiness/harness.py": hashlib.sha256(HARNESS_PATH.read_bytes()).hexdigest(),
            "datasets/router_taskb.jsonl": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
        },
        "started_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "resume_windows": resume_windows,
    }
    print(json.dumps(provenance, indent=2), flush=True)
    if vm["location"].casefold() != "swedencentral":
        raise SystemExit(f"VM is in {vm['location']}, not the deployment region; refusing to measure.")

    todo = [item for item in prompts if item["id"] not in completed_prompts]
    total_pairs = len(todo) * (args.iterations + 1)
    done = 0
    with raw_path.open("a", encoding="utf-8") as stream:
        for prompt_index, item in enumerate(todo):
            for pair_index in range(args.iterations + 1):
                warmup = pair_index == 0
                router_first = pair_index % 2 == 0
                order = (ROUTER_ARM, DIRECT_ARM) if router_first else (DIRECT_ARM, ROUTER_ARM)
                for position, arm in enumerate(order, start=1):
                    result = measure(harness, client, arm, item)
                    record = {
                        "run_id": run_id,
                        "arm": arm,
                        "side": "router" if arm == ROUTER_ARM else "direct",
                        "question_id": item["id"],
                        "tier": item["tier"],
                        "category": item["category"],
                        "source": item.get("source"),
                        "pair_index": pair_index,
                        "iteration": pair_index,
                        "warmup": warmup,
                        "position_in_pair": position,
                        "router_first": router_first,
                        "api": "chat",
                        "reasoning_effort": None,
                        "max_output_tokens": MAX_OUTPUT_TOKENS,
                        "answer_budget": ANSWER_BUDGET,
                        "tools_enabled": False,
                        "region": "swedencentral",
                        "client_location": "swedencentral-linux-vm",
                        "deployment_type": "paygo",
                        "endpoint_host": endpoint_host,
                        **result,
                    }
                    stream.write(json.dumps(record, ensure_ascii=False) + "\n")
                    stream.flush()
                    served = (result.get("model_actually_served") or "?")
                    print(f"{'WU ' if warmup else '   '}[{done + 1:>3}/{total_pairs}] {item['id']:<6} "
                          f"{arm:<22} pos={position} served={served:<26} "
                          f"TTFT={result.get('ttft_ms')} E2E={result.get('e2e_ms')} "
                          f"err={result.get('error')}", flush=True)
                    time.sleep(SLEEP_BETWEEN_REQUESTS_S)
                done += 1
    provenance["finished_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    provenance["raw_file"] = raw_path.name
    provenance["raw_sha256"] = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    provenance["raw_records"] = sum(1 for _ in raw_path.open(encoding="utf-8"))
    (out_dir / f"provenance_paired_{run_id}.json").write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("DONE", raw_path, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
