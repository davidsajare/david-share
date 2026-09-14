#!/usr/bin/env python3
"""
Multi-turn session runner for the Qira scenarios.

Every earlier study sent independent single-turn requests and said so; Qira is a
conversational assistant, so the production quantities are per-session: how
prompt tokens grow as history is re-sent, whether prompt caching absorbs that
growth, what a whole conversation costs, how TTFT moves from turn 1 to turn N,
and whether Model Router keeps the same model across a conversation.

Each session in datasets/qira_sessions.jsonl is a scripted conversation whose
turns depend on the previous answers. The runner replays it statelessly: turn k
sends the system message, the k-1 prior user/assistant pairs (using the model's
own earlier answers), and the new user message. Responses and Chat Completions
receive identical context. Measurement is harness.run_one, unchanged.

Usage (same-region VM):
    python sessions.py --dataset datasets/qira_sessions.jsonl \
        --arms gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat,router-sol-luna-quality#chat \
        --iterations 3 --region swedencentral --client-location swedencentral-linux-vm

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import harness

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs"


def parse_arm(spec: str) -> dict:
    """'deployment[@effort][#api]' -> arm dict; '-' or absent effort = not sent."""
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


def load_sessions(path: Path) -> list[dict]:
    """Session records carry `turns`, not `text`; validate the shape the runner relies on."""
    sessions = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not sessions:
        raise SystemExit(f"{path} contained no sessions.")
    for s in sessions:
        turns = s.get("turns") or []
        if not s.get("id") or not s.get("scenario") or not turns:
            raise SystemExit(f"{path.name}: session {s.get('id')!r} needs id, scenario and turns")
        if [t.get("turn") for t in turns] != list(range(1, len(turns) + 1)):
            raise SystemExit(f"{path.name}: session {s['id']} turns must be numbered 1..N in order")
        for t in turns:
            if not t.get("text") or not isinstance(t.get("max_output_tokens"), int):
                raise SystemExit(f"{path.name}: session {s['id']} turn {t.get('turn')} needs text and integer max_output_tokens")
    return sessions


def main() -> int:
    p = argparse.ArgumentParser(description="Replay scripted multi-turn Qira sessions and record per-turn measurements.")
    p.add_argument("--dataset", required=True)
    p.add_argument("--arms", required=True, help="comma-separated deployment[@effort][#api]")
    p.add_argument("--iterations", type=int, default=3)
    p.add_argument("--region", required=True)
    p.add_argument("--client-location", default="unknown", dest="client_location")
    p.add_argument("--deployment-type", default="paygo", dest="deployment_type")
    p.add_argument("--pricing", default=str(harness.DEFAULT_PRICING))
    p.add_argument("--models", default=str(harness.DEFAULT_MODELS))
    args = p.parse_args()

    sessions = load_sessions(Path(args.dataset))
    pricing = harness.load_pricing(Path(args.pricing))
    registry = harness.load_model_registry(Path(args.models))
    arms = [parse_arm(s.strip()) for s in args.arms.split(",") if s.strip()]

    client, endpoint = harness.build_client()
    endpoint_host = urlparse(endpoint).netloc or endpoint
    rtt = harness.measure_rtt(endpoint_host)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / f"sessions_{stamp}.jsonl"
    total = len(arms) * len(sessions) * args.iterations * sum(len(s["turns"]) for s in sessions) // len(sessions)
    print(f"region={args.region} endpoint={endpoint_host} client={args.client_location} rtt={rtt}ms")
    print(f"arms={[a['arm_id'] for a in arms]} sessions={len(sessions)} iterations={args.iterations} -> {total} turns")
    print("tools: NONE; history replayed statelessly on both API surfaces; sdk retries: 0")
    print(f"writing {out_path}\n")

    done = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for arm in arms:
            for session in sessions:
                for iteration in range(1, args.iterations + 1):
                    history: list[dict] = []
                    cumulative_cost = 0.0
                    cumulative_tokens = 0
                    served_sequence: list[str] = []
                    for turn in session["turns"]:
                        cap = int(turn["max_output_tokens"]) + harness.REASONING_HEADROOM_UNIFORM
                        item = {"id": f"{session['id']}-T{turn['turn']}", "text": turn["text"]}
                        m = harness.run_one(client, arm["deployment"], item, cap, arm["effort"],
                                            api=arm["api"], history=list(history))
                        done += 1
                        served = m["model_actually_served"]
                        billing = harness.billing_model_name(served)
                        routing = m.get("routing") or {}
                        cost = harness.compute_cost(pricing, billing or arm["deployment"], m["prompt_tokens"],
                                                    m["cached_tokens"], m["completion_tokens"], registry)
                        valid = not m["error"] and m["status"] == "completed" and not m["truncated"]
                        if valid and cost is not None:
                            cumulative_cost += cost
                        cumulative_tokens += m["prompt_tokens"] + m["completion_tokens"]
                        if billing:
                            served_sequence.append(billing.replace("gpt-5.6-", ""))
                        record = {
                            "run_id": stamp, "kind": "session", "arm": arm["arm_id"], "model_requested": arm["deployment"],
                            "reasoning_effort": arm["effort"], "api": arm["api"],
                            "session_id": session["id"], "scenario": session["scenario"], "iteration": iteration,
                            "turn": turn["turn"], "turns_in_session": len(session["turns"]), "task_type": turn["task_type"],
                            "history_messages": len(history), "history_chars": sum(len(h["content"]) for h in history),
                            "region": args.region, "endpoint_host": endpoint_host, "deployment_type": args.deployment_type,
                            "client_location": args.client_location, "network_rtt_ms": rtt, "tools_enabled": False,
                            "warmup": False,
                            "model_actually_served": served, "served_model_family": billing,
                            "router_mode": routing.get("mode"), "router_latency_ms": routing.get("router_latency_ms"),
                            "router_fallback": routing.get("fallback"),
                            "answer_budget": int(turn["max_output_tokens"]), "max_output_tokens": cap,
                            **{k: m[k] for k in ("ttft_ms", "e2e_ms", "decode_ms", "tpot_ms", "decode_tps", "prompt_tokens",
                                                 "cached_tokens", "completion_tokens", "reasoning_tokens", "status", "truncated",
                                                 "incomplete_reason", "error")},
                            "finish_reason": m.get("finish_reason"),
                            "cost_usd": cost, "session_cost_usd_so_far": round(cumulative_cost, 8),
                            "session_tokens_so_far": cumulative_tokens,
                            "served_sequence_so_far": ">".join(served_sequence),
                            "response_chars": len(m["response_text"]),
                            "response_sha256": hashlib.sha256(m["response_text"].encode()).hexdigest(),
                            "response_text": m["response_text"],
                        }
                        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                        fh.flush()
                        tag = "OK " if valid else "BAD"
                        print(f"  {tag} [{done}/{total}] {arm['arm_id']:<28} {session['id']:<8} it{iteration} T{turn['turn']} "
                              f"hist={m['prompt_tokens']:>5}tok cached={m['cached_tokens']:>4} TTFT={m['ttft_ms'] or 0:>6.0f}ms "
                              f"out={m['completion_tokens']:>4} served={billing or '-'}" + (f" ERR {m['error'][:50]}" if m["error"] else ""))
                        if not valid:
                            # A broken turn would poison every later turn's context; stop this session replay.
                            break
                        history.append({"role": "user", "content": turn["text"]})
                        history.append({"role": "assistant", "content": m["response_text"]})
                    time.sleep(0.5)
    print(f"\nDone. {done} turns → {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
