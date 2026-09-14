#!/usr/bin/env python3
"""
Day-1 probe for Task B: does the Model Router expose which model it served?

The missing served-model identity is the first risk of the router workstream. Before
spending VM time on hundreds of routed calls we need to know, per API surface,
where the served model shows up and whether the routing trace is available.

Checks, per router deployment:
  1. Responses API, stream=True   -> response.model on response.completed
  2. Responses API, stream=False  -> response.model
  3. Chat Completions, no stream  -> completion.model, plus the preview
     routing trace behind the `Foundry-Features: ModelRouterControls=V1Preview`
     header (model_selection_details.model_router_details)
  4. reasoning_effort passthrough -> does the router accept reasoning.effort and
     does the served model / reasoning_tokens change with it

Usage:
  python probe_router.py --deployments router-sol-luna-balanced,router-sol-luna-cost
  python probe_router.py --deployments router-sol-luna-quality --prompt "..." --effort high

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import harness

ROUTER_TRACE_HEADER = {"Foundry-Features": "ModelRouterControls=V1Preview"}

SIMPLE_PROMPT = "What is the capital of France? Answer in one word."
COMPLEX_PROMPT = (
    "A warehouse robot fleet has 12 robots. Each robot can carry 40 kg and moves at "
    "1.5 m/s. Orders arrive at 90 per hour, each averaging 25 kg and requiring a 120 m "
    "round trip plus 45 s of handling. Determine the fleet utilization, whether the fleet "
    "keeps up with demand, and the minimum fleet size for 80% utilization. Show the "
    "reasoning step by step and state every assumption you make."
)


def _dump_selection(obj) -> dict | None:
    """Pull model_selection_details out of an SDK object without assuming its shape."""
    sel = getattr(obj, "model_selection_details", None)
    if sel is None and hasattr(obj, "model_extra"):
        sel = (obj.model_extra or {}).get("model_selection_details")
    if sel is None:
        return None
    if hasattr(sel, "model_dump"):
        return sel.model_dump()
    return sel


def probe_responses(client, deployment: str, prompt: str, effort: str | None, stream: bool) -> dict:
    kwargs = {
        "model": deployment,
        "input": [{"role": "user", "content": prompt}],
        "max_output_tokens": 4000,
        "stream": stream,
    }
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    t0 = time.perf_counter()
    out = {"surface": f"responses/{'stream' if stream else 'sync'}", "effort": effort}
    try:
        if stream:
            resp = None
            ttft = None
            for event in client.responses.create(**kwargs):
                if getattr(event, "type", None) == "response.output_text.delta" and ttft is None:
                    ttft = time.perf_counter() - t0
                if getattr(event, "type", None) in ("response.completed", "response.incomplete"):
                    resp = event.response
            out["ttft_ms"] = round(ttft * 1000, 1) if ttft else None
        else:
            resp = client.responses.create(**kwargs)
        out["e2e_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["model"] = getattr(resp, "model", None)
        out["status"] = getattr(resp, "status", None)
        p, c, cached, r = harness._extract_usage(resp)
        out["usage"] = {"in": p, "out": c, "cached": cached, "reasoning": r}
        out["selection"] = _dump_selection(resp)
        extra_keys = sorted((getattr(resp, "model_extra", None) or {}).keys())
        if extra_keys:
            out["extra_keys"] = extra_keys
    except Exception as exc:  # noqa: BLE001 - probe must report, not die
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return out


def probe_chat(client, deployment: str, prompt: str, effort: str | None, with_header: bool) -> dict:
    kwargs = {
        "model": deployment,
        "messages": [{"role": "user", "content": prompt}],
        "max_completion_tokens": 4000,
    }
    if effort:
        kwargs["reasoning_effort"] = effort
    if with_header:
        kwargs["extra_headers"] = ROUTER_TRACE_HEADER
    t0 = time.perf_counter()
    out = {"surface": f"chat/{'trace-header' if with_header else 'plain'}", "effort": effort}
    try:
        resp = client.chat.completions.create(**kwargs)
        out["e2e_ms"] = round((time.perf_counter() - t0) * 1000, 1)
        out["model"] = getattr(resp, "model", None)
        usage = resp.usage
        reasoning = 0
        det = getattr(usage, "completion_tokens_details", None)
        if det is not None:
            reasoning = getattr(det, "reasoning_tokens", 0) or 0
        out["usage"] = {"in": usage.prompt_tokens, "out": usage.completion_tokens, "reasoning": reasoning}
        out["selection"] = _dump_selection(resp)
        extra_keys = sorted((getattr(resp, "model_extra", None) or {}).keys())
        if extra_keys:
            out["extra_keys"] = extra_keys
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {str(exc)[:300]}"
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Probe Model Router served-model visibility.")
    p.add_argument("--deployments", required=True, help="comma-separated router deployment names")
    p.add_argument("--prompt", default=None, help="override prompt (default: one simple + one complex)")
    p.add_argument("--effort", default=None, help="single reasoning_effort to test (default: none, then high)")
    p.add_argument("--chat-api-version", default="2024-10-21",
                   help="api-version for the Chat Completions trace-header probe")
    args = p.parse_args()

    client, endpoint = harness.build_client()
    chat_client, _ = harness.build_client(api_version=args.chat_api_version)
    print(f"endpoint={endpoint}  responses api-version={harness.API_VERSION}  "
          f"chat api-version={args.chat_api_version}")

    prompts = [("simple", args.prompt)] if args.prompt else [("simple", SIMPLE_PROMPT), ("complex", COMPLEX_PROMPT)]
    efforts = [args.effort] if args.effort else [None, "high"]

    results = []
    for dep in [d.strip() for d in args.deployments.split(",") if d.strip()]:
        print(f"\n=== {dep}")
        for label, prompt in prompts:
            for effort in efforts:
                for res in (
                    probe_responses(client, dep, prompt, effort, stream=True),
                    probe_responses(client, dep, prompt, effort, stream=False),
                    probe_chat(chat_client, dep, prompt, effort, with_header=True),
                    probe_chat(chat_client, dep, prompt, effort, with_header=False),
                ):
                    res.update({"deployment": dep, "prompt": label})
                    results.append(res)
                    sel = res.get("selection")
                    sel_txt = json.dumps(sel, ensure_ascii=False)[:220] if sel else "-"
                    print(f"  {label:7s} effort={str(effort):5s} {res['surface']:22s} "
                          f"model={res.get('model')!s:14s} usage={res.get('usage')} "
                          f"err={res.get('error') or '-'}")
                    if sel:
                        print(f"      selection: {sel_txt}")

    print("\n--- verdict")
    served = {r.get("model") for r in results if r.get("model")}
    print(f"distinct response.model values seen: {sorted(served)}")
    router_echo = [r for r in results if r.get("model") in {r["deployment"] for r in results}]
    if router_echo:
        print("[WARN] some surfaces echoed the router deployment name -> served model NOT exposed there")
    with_trace = [r["surface"] for r in results if r.get("selection")]
    print(f"routing trace available on: {sorted(set(with_trace)) or 'NONE'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
