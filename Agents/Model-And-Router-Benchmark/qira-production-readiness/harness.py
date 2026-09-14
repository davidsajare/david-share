#!/usr/bin/env python3
"""
Unified benchmark harness for the Qira scenario studies.

Serves two workstreams from one codebase and one log schema:

  Task A  语言模型基准测试        --mode direct
          Runs each dataset item against each candidate deployment directly.

  Task B  Model Router 路由验证   --mode router
          Runs each dataset item against the Model Router deployment and
          records which model was ACTUALLY served.

Methodology carried over from david-share/Agents/AOAI-Model-Migration-Benchmark
(scenario S1 "Direct AOAI", the no-Bing path): Responses API with stream=True,
TTFT measured at first output-text delta, token usage read from the
response.completed event.

Three fairness constraints are enforced here:

  1. NO WEB SEARCH. No tools are ever attached. Every record carries
     tools_enabled=false so the report can prove the comparison was native-only.
  2. SAME REGION. --region is required and stamped on every record alongside the
     endpoint host; analyze.py refuses to aggregate across regions or endpoints.
  3. ALIGNED reasoning_effort. Pinned per model to its documented minimum via
     config/models.json, so reasoning and non-reasoning models are comparable.

Both phases are measured separately:
  prefill → ttft_ms
  decode  → decode_ms, tpot_ms, decode_tps

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
DEFAULT_PRICING = ROOT / "config" / "pricing.json"
DEFAULT_MODELS = ROOT / "config" / "models.json"
OUTPUT_DIR = ROOT / "outputs"

API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

# Model Router (2025-11-18) surfaces its routing decision only on Chat
# Completions, and only when this preview header is sent. Probed 2026-09-10 in
# swedencentral: the Responses API returns "The requested operation is
# unsupported" for router deployments, so Task B must use Chat Completions.
ROUTER_TRACE_HEADER = {"Foundry-Features": "ModelRouterControls=V1Preview"}

SYSTEM_MSG = (
    "You are a system-level cross-device AI assistant. Answer the user directly "
    "and concisely. Do not ask clarifying questions."
)

# max_output_tokens covers reasoning tokens AND the answer. The dataset caps are
# sized for the answer alone, so reasoning must be budgeted on top or high-effort
# arms get truncated mid-answer and the comparison silently becomes unfair.
# Measured on gpt-5.6-luna: a 900-token cap at effort=high spent 323 tokens on
# reasoning before emitting a word, and the answer was cut off.
#
# The headroom is deliberately UNIFORM across arms rather than scaled per effort.
# A per-effort headroom would hand high-effort arms a longer answer ceiling than
# low-effort ones, trading one bias for another. With one cap per item, every arm
# gets the same ceiling and any difference in output length is genuine model
# verbosity - which is exactly the cost signal we are trying to measure.
REASONING_HEADROOM_UNIFORM = 8192


# --------------------------------------------------------------------------
# Dataset / pricing loading
# --------------------------------------------------------------------------

def load_jsonl(path: Path) -> list[dict]:
    items = []
    with path.open("r", encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{path.name}:{lineno} is not valid JSON — {exc}") from exc
    if not items:
        raise SystemExit(f"{path} contained no records.")
    for i, item in enumerate(items):
        if "id" not in item or "text" not in item:
            raise SystemExit(f"{path.name} record {i} is missing a required 'id' or 'text' field.")
    return items


def load_pricing(path: Path) -> dict:
    if not path.exists():
        print(f"[warn] pricing file not found at {path}; cost will be reported as N/A")
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("models", {})


def load_model_registry(path: Path) -> dict:
    """deployment/model name -> {family, reasoning_effort}."""
    if not path.exists():
        print(f"[warn] model registry not found at {path}; reasoning_effort will not be pinned")
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {_normalize(k): v for k, v in data.get("models", {}).items()}


def compute_cost(pricing: dict, model: str, prompt_tokens: int,
                 cached_tokens: int, completion_tokens: int, registry: dict | None = None):
    """
    Return USD cost, or None when this model has no confirmed pricing.

    Deployment names need not match billing model names (a deployment called
    gpt-4o-mini-bench still bills as gpt-4o-mini), so the registry's model_name
    is consulted before giving up.
    """
    key = _normalize(model)
    entry = pricing.get(key)
    if not entry and registry:
        alias = (registry.get(key, {}) or {}).get("model_name")
        if alias:
            entry = pricing.get(_normalize(alias))
    if not entry:
        return None
    if any(entry.get(k) is None for k in ("input", "cached", "output")):
        return None
    fresh_input = max(prompt_tokens - cached_tokens, 0)
    cost = (
        fresh_input / 1_000_000 * entry["input"]
        + cached_tokens / 1_000_000 * entry["cached"]
        + completion_tokens / 1_000_000 * entry["output"]
    )
    return round(cost, 8)


def _normalize(model: str) -> str:
    return (model or "").strip().lower()


def parse_efforts(spec: str | None) -> list[str | None] | None:
    """'none,low,-' -> ['none', 'low', None]; None when the flag is absent."""
    if spec is None:
        return None
    efforts: list[str | None] = []
    for raw in spec.split(","):
        raw = raw.strip()
        if not raw:
            continue
        efforts.append(None if raw == "-" else raw)
    if not efforts:
        raise SystemExit("--efforts was given but named no values.")
    return efforts


def build_arms(targets: list[str], registry: dict, matrix: bool,
               efforts_override: list[str | None] | None = None) -> list[dict]:
    """
    Expand deployments into test arms.

    A model and a reasoning_effort together define a distinct cost/latency/quality
    operating point, so the arm - not the model - is the unit of comparison.
    gpt-5.6-luna at 'none' and at 'max' are effectively different products.

    matrix=True  → every supported effort per deployment (the full permutation)
    matrix=False → one arm per deployment at its pinned reasoning_effort
    efforts_override → the same explicit list for every deployment; used by
                       Task B where router and direct arms must share efforts
    """
    arms = []
    for dep in targets:
        entry = registry.get(_normalize(dep), {}) or {}
        supported = entry.get("supported_efforts")

        if efforts_override is not None:
            efforts = list(efforts_override)
        elif matrix:
            if supported is None:
                raise SystemExit(
                    f"'{dep}' has no supported_efforts in the model registry.\n"
                    "Run probe_efforts.py --write first, so the matrix is built from\n"
                    "what the endpoint actually accepts rather than a guess."
                )
            efforts = list(supported) if supported else [None]
        else:
            efforts = [entry.get("reasoning_effort")]

        for effort in efforts:
            arms.append({
                "deployment": dep,
                "effort": effort,
                "arm_id": f"{dep}@{effort}" if effort else dep,
                "family": entry.get("family"),
            })
    return arms


# --------------------------------------------------------------------------
# Response parsing
# --------------------------------------------------------------------------

def _extract_usage(resp):
    """
    Pull token counts off a Responses API response object, defensively.

    reasoning_tokens matters for the decode math: on reasoning models they are
    billed and generated as output tokens but never surface as text deltas, so
    a naive tokens-per-second figure would be wrong.
    """
    prompt = completion = cached = reasoning = 0
    usage = getattr(resp, "usage", None)
    if usage:
        prompt = getattr(usage, "input_tokens", 0) or 0
        completion = getattr(usage, "output_tokens", 0) or 0
        in_details = getattr(usage, "input_tokens_details", None)
        if in_details:
            cached = getattr(in_details, "cached_tokens", 0) or 0
        out_details = getattr(usage, "output_tokens_details", None)
        if out_details:
            reasoning = getattr(out_details, "reasoning_tokens", 0) or 0
    return prompt, completion, cached, reasoning


def _extract_served_model(resp) -> str | None:
    """
    Task B hinges on this: which model did the router actually serve?

    The Responses API echoes the serving model on the response object. When a
    Model Router deployment resolves to an underlying model, that resolved name
    is what surfaces here rather than the router deployment name. If this ever
    comes back as the router deployment name itself, the router is not exposing
    its choice and the documented fallback plan applies.
    """
    for attr in ("model", "model_name", "deployment"):
        value = getattr(resp, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


_MODEL_VERSION_SUFFIX = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def billing_model_name(served: str | None) -> str | None:
    """
    Strip the version suffix the router appends ("gpt-5.6-luna-2026-07-09"),
    so the served model lines up with pricing.json and the model registry.
    """
    if not served:
        return served
    return _MODEL_VERSION_SUFFIX.sub("", served.strip())


def _extract_routing_trace(resp) -> dict | None:
    """
    Model Router's routing decision, from the Chat Completions preview header.

    Shape (2025-11-18):
      {"model_router_details": {"mode": "balanced",
        "routing_trace": [{"latency_ms": 19,
                           "attempts": [{"model": "gpt-5.6-luna", "result": {"status": 200}}]}]}}
    Returned flattened so analyze.py can read it without knowing the SDK object.
    """
    sel = getattr(resp, "model_selection_details", None)
    if sel is None:
        sel = (getattr(resp, "model_extra", None) or {}).get("model_selection_details")
    if sel is None:
        return None
    if hasattr(sel, "model_dump"):
        sel = sel.model_dump()
    details = (sel or {}).get("model_router_details") or {}
    trace = details.get("routing_trace") or []
    attempts = []
    router_latency_ms = None
    for hop in trace:
        if router_latency_ms is None and hop.get("latency_ms") is not None:
            router_latency_ms = hop["latency_ms"]
        for att in hop.get("attempts") or []:
            attempts.append({
                "model": att.get("model"),
                "status": (att.get("result") or {}).get("status"),
                "error": (att.get("result") or {}).get("error"),
            })
    return {
        "mode": details.get("mode"),
        "router_latency_ms": router_latency_ms,
        "attempts": attempts,
        "fallback": len(attempts) > 1,
    }


def measure_rtt(host: str, samples: int = 7) -> float | None:
    """
    TCP connect time to the endpoint, as a network-latency floor.

    TTFT bundles network round-trip, queuing, prefill and first-token time. To
    argue about model performance we need to know how much of it was the
    network. Running from a VM in the same region should put this in the
    low single-digit milliseconds; a cross-Pacific client shows 100-200ms
    (see AOAI-Model-Migration-Benchmark, TTFT Composition).
    """
    import socket

    timings = []
    for _ in range(samples):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        start = time.perf_counter()
        try:
            sock.connect((host, 443))
            timings.append((time.perf_counter() - start) * 1000)
        except OSError:
            pass
        finally:
            sock.close()
    if not timings:
        return None
    return round(statistics.median(timings), 2)


# --------------------------------------------------------------------------
# Single request
# --------------------------------------------------------------------------

def run_one(client, deployment: str, item: dict, max_output_tokens: int,
            reasoning_effort: str | None, api: str = "responses",
            history: list[dict] | None = None):
    """
    Execute one streamed request and return a raw measurement dict.

    No tools are attached. This is the S1 "Direct AOAI" path from
    AOAI-Model-Migration-Benchmark - native model capability only, no web search.

    api="responses" is the Task A surface. api="chat" is the Chat Completions
    surface Task B needs: Model Router deployments reject the Responses API and
    only expose their routing trace on Chat Completions (see ROUTER_TRACE_HEADER).
    Direct baselines in a router run use the same surface so the TTFT overhead
    attributed to the router is not an API-surface artefact.

    history: prior {"role","content"} turns of a multi-turn session, inserted
    between the system message and this turn's user message. Stateless on both
    API surfaces (no previous_response_id), so Responses and Chat see identical
    context and the prompt-token growth per turn is directly comparable.

    Phase split:
      ttft_ms     prefill + network + queuing, up to the first text delta
      decode_ms   first text delta -> stream end
      tpot_ms     decode_ms per emitted output token
    """
    if api == "chat":
        return _run_one_chat(client, deployment, item, max_output_tokens, reasoning_effort, history)

    t0 = time.perf_counter()
    ttft = None
    last_delta_at = None
    text_parts: list[str] = []
    prompt_tokens = completion_tokens = cached_tokens = reasoning_tokens = 0
    served_model = None
    error = None
    status = None
    incomplete_reason = None

    kwargs = {
        "model": deployment,
        "input": [
            {"role": "system", "content": SYSTEM_MSG},
            *(history or []),
            {"role": "user", "content": item["text"]},
        ],
        "stream": True,
        "max_output_tokens": max_output_tokens,
    }
    if reasoning_effort:
        kwargs["reasoning"] = {"effort": reasoning_effort}

    try:
        stream = client.responses.create(**kwargs)
        for event in stream:
            etype = getattr(event, "type", None)
            if etype == "response.output_text.delta":
                now = time.perf_counter()
                if ttft is None:
                    ttft = now - t0
                last_delta_at = now
                text_parts.append(getattr(event, "delta", "") or "")
            elif etype in ("response.completed", "response.incomplete", "response.failed"):
                # response.incomplete carries full usage too. Listening only for
                # 'completed' silently drops every truncated run, which is exactly
                # the high-reasoning-effort case we most need to measure.
                resp = getattr(event, "response", None)
                if resp is not None:
                    (prompt_tokens, completion_tokens,
                     cached_tokens, reasoning_tokens) = _extract_usage(resp)
                    served_model = _extract_served_model(resp)
                    status = getattr(resp, "status", None)
                    details = getattr(resp, "incomplete_details", None)
                    if details is not None:
                        incomplete_reason = getattr(details, "reason", None)
    except Exception as exc:  # noqa: BLE001 - benchmark must survive any provider error
        error = f"{type(exc).__name__}: {exc}"

    # A stream that never carried usage cannot be billed or compared; do not let it
    # pass as a zero-token "completed" answer.
    if not error and status == "completed" and prompt_tokens == 0:
        status = "incomplete"
        incomplete_reason = incomplete_reason or "missing_usage"
        error = "Responses stream completed without usage; record is not billable"

    e2e = time.perf_counter() - t0

    # Decode window = first text delta -> last text delta. Reasoning tokens are
    # produced before any text surfaces, so they belong to the pre-TTFT window
    # and are excluded from the decode rate.
    decode_ms = tpot_ms = decode_tps = None
    if ttft is not None and last_delta_at is not None:
        decode_s = last_delta_at - (t0 + ttft)
        emitted = max(completion_tokens - reasoning_tokens, 0)
        decode_ms = round(decode_s * 1000, 1)
        if emitted > 1 and decode_s > 0:
            tpot_ms = round(decode_s * 1000 / (emitted - 1), 2)
            decode_tps = round((emitted - 1) / decode_s, 2)

    return {
        "ttft_ms": round(ttft * 1000, 1) if ttft is not None else None,
        "e2e_ms": round(e2e * 1000, 1),
        "decode_ms": decode_ms,
        "tpot_ms": tpot_ms,
        "decode_tps": decode_tps,
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "status": status,
        "truncated": incomplete_reason == "max_output_tokens",
        "incomplete_reason": incomplete_reason,
        "model_actually_served": served_model,
        "routing": None,
        "response_text": "".join(text_parts).strip(),
        "error": error,
    }


def _run_one_chat(client, deployment: str, item: dict, max_output_tokens: int,
                  reasoning_effort: str | None, history: list[dict] | None = None):
    """Chat Completions twin of run_one: same phase split, same record shape."""
    t0 = time.perf_counter()
    ttft = None
    last_delta_at = None
    text_parts: list[str] = []
    prompt_tokens = completion_tokens = cached_tokens = reasoning_tokens = 0
    served_model = None
    routing = None
    error = None
    finish_reason = None

    kwargs = {
        "model": deployment,
        "messages": [
            {"role": "system", "content": SYSTEM_MSG},
            *(history or []),
            {"role": "user", "content": item["text"]},
        ],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": max_output_tokens,
        "extra_headers": ROUTER_TRACE_HEADER,
    }
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort

    try:
        stream = client.chat.completions.create(**kwargs)
        for chunk in stream:
            model = getattr(chunk, "model", None)
            if isinstance(model, str) and model.strip():
                served_model = model.strip()
            trace = _extract_routing_trace(chunk)
            if trace is not None:
                routing = trace
            for choice in getattr(chunk, "choices", None) or []:
                delta = getattr(choice, "delta", None)
                content = getattr(delta, "content", None) if delta is not None else None
                if content:
                    now = time.perf_counter()
                    if ttft is None:
                        ttft = now - t0
                    last_delta_at = now
                    text_parts.append(content)
                if getattr(choice, "finish_reason", None):
                    finish_reason = choice.finish_reason
            usage = getattr(chunk, "usage", None)
            if usage is not None:
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                pd = getattr(usage, "prompt_tokens_details", None)
                if pd is not None:
                    cached_tokens = getattr(pd, "cached_tokens", 0) or 0
                cd = getattr(usage, "completion_tokens_details", None)
                if cd is not None:
                    reasoning_tokens = getattr(cd, "reasoning_tokens", 0) or 0
    except Exception as exc:  # noqa: BLE001 - benchmark must survive any provider error
        error = f"{type(exc).__name__}: {exc}"

    e2e = time.perf_counter() - t0

    decode_ms = tpot_ms = decode_tps = None
    if ttft is not None and last_delta_at is not None:
        decode_s = last_delta_at - (t0 + ttft)
        emitted = max(completion_tokens - reasoning_tokens, 0)
        decode_ms = round(decode_s * 1000, 1)
        if emitted > 1 and decode_s > 0:
            tpot_ms = round(decode_s * 1000 / (emitted - 1), 2)
            decode_tps = round((emitted - 1) / decode_s, 2)

    truncated = finish_reason == "length"
    incomplete_reason = "max_output_tokens" if truncated else None
    if not error and finish_reason not in ("stop", "length"):
        incomplete_reason = finish_reason or "missing_finish_reason"
        error = f"Chat response did not complete normally: {incomplete_reason}"
    if not error and prompt_tokens == 0:
        # stream_options.include_usage was requested; a stream without the usage
        # chunk cannot be costed and must not be counted as a completed answer.
        incomplete_reason = "missing_usage"
        error = "Chat stream ended without a usage chunk; record is not billable"
    return {
        "ttft_ms": round(ttft * 1000, 1) if ttft is not None else None,
        "e2e_ms": round(e2e * 1000, 1),
        "decode_ms": decode_ms,
        "tpot_ms": tpot_ms,
        "decode_tps": decode_tps,
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "status": ("incomplete" if truncated else "completed") if not error else None,
        "truncated": truncated,
        "incomplete_reason": incomplete_reason,
        "finish_reason": finish_reason,
        "model_actually_served": served_model,
        "routing": routing,
        "response_text": "".join(text_parts).strip(),
        "error": error,
    }


# --------------------------------------------------------------------------
# Benchmark loop
# --------------------------------------------------------------------------

def build_client(api_version: str | None = None):
    """
    Azure OpenAI client. Prefers Entra ID; falls back to key auth.

    Entra is not just a preference here: resources with disableLocalAuth=true
    reject key auth outright, and keeping keys off disk is the better posture
    for a folder that syncs to OneDrive.

    api_version overrides the module default; the Model Router routing-trace
    header is documented against the Chat Completions surface on 2024-10-21.
    """
    api_version = api_version or API_VERSION
    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    if not endpoint:
        raise SystemExit(
            "Missing AZURE_OPENAI_ENDPOINT.\n"
            "Copy .env.example, fill it in, and load it before running."
        )
    try:
        from openai import AzureOpenAI
    except ImportError as exc:
        raise SystemExit("The 'openai' package is not installed. Run: pip install -r requirements.txt") from exc

    key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    # max_retries=0: a benchmark must see every HTTP failure as a recorded error.
    # The SDK default (2 silent retries with backoff) would hide 429/5xx inside TTFT.
    if key:
        return AzureOpenAI(azure_endpoint=endpoint, api_key=key,
                           api_version=api_version, max_retries=0), endpoint

    try:
        from azure.identity import DefaultAzureCredential, get_bearer_token_provider
    except ImportError as exc:
        raise SystemExit(
            "No AZURE_OPENAI_API_KEY set and 'azure-identity' is not installed.\n"
            "Run: pip install -r requirements.txt"
        ) from exc

    token_provider = get_bearer_token_provider(
        DefaultAzureCredential(), "https://cognitiveservices.azure.com/.default")
    return AzureOpenAI(azure_endpoint=endpoint, azure_ad_token_provider=token_provider,
                       api_version=api_version, max_retries=0), endpoint


def run_benchmark(args) -> Path:
    dataset = load_jsonl(Path(args.dataset))
    pricing = load_pricing(Path(args.pricing))
    registry = load_model_registry(Path(args.models))

    targets = [d.strip() for d in args.deployments.split(",") if d.strip()]
    if not targets:
        raise SystemExit("--deployments must name at least one deployment.")

    arms = build_arms(targets, registry, args.matrix, parse_efforts(args.efforts))

    client, endpoint = build_client()
    endpoint_host = urlparse(endpoint).netloc or endpoint

    # Same-region constraint: every model must be compared on one endpoint in one
    # region, otherwise network RTT differences contaminate the latency numbers.
    print(f"\nregion={args.region}  endpoint={endpoint_host}  deployment_type={args.deployment_type}")
    print(f"client={args.client_location}")
    print("tools: NONE (native capability only, no web search)")

    rtt = measure_rtt(endpoint_host)
    if rtt is None:
        print("network RTT: could not measure")
    else:
        verdict = "same-region" if rtt < 15 else ("regional" if rtt < 50 else "REMOTE — network dominates TTFT")
        print(f"network RTT: {rtt}ms median TCP connect  → {verdict}")
        if rtt >= 50:
            print("  [warn] Run this from a VM in the same region as the endpoint.")
            print("         See scripts/provision-benchmark-vm.sh")

    print(f"\narms ({len(arms)}):")
    for a in arms:
        print(f"  {a['arm_id']:<28} effort={a['effort'] or 'not sent'}")

    api = args.api
    if args.mode == "router" and api != "chat":
        print("[note] --mode router forces --api chat: router deployments reject the Responses API")
        api = "chat"
    print(f"api surface: {api} (streaming)")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    out_path = OUTPUT_DIR / f"{args.mode}_{stamp}.jsonl"

    total = len(arms) * len(dataset) * (args.iterations + args.warmup)
    done = 0
    print(f"\nmode={args.mode}  items={len(dataset)}  "
          f"iterations={args.iterations} (+{args.warmup} warmup)  → {total} requests")
    print(f"writing {out_path}\n")

    with out_path.open("w", encoding="utf-8") as fh:
        for arm in arms:
            deployment = arm["deployment"]
            effort = arm["effort"]
            for item in dataset:
                answer_budget = int(item.get("max_output_tokens", args.max_output_tokens))
                cap = answer_budget + REASONING_HEADROOM_UNIFORM
                for i in range(1, args.iterations + args.warmup + 1):
                    is_warmup = i <= args.warmup
                    m = run_one(client, deployment, item, cap, effort, api=api)
                    done += 1

                    served = m["model_actually_served"]
                    billing_model = billing_model_name(served)
                    routing = m.get("routing") or {}
                    record = {
                        "run_id": stamp,
                        "question_id": item["id"],
                        "scenario": item.get("scenario"),
                        "scenario_label": item.get("scenario_label"),
                        "scope": item.get("scope"),
                        "complexity_tag": item.get("tier"),
                        "task_type": item.get("task_type"),
                        "category": item.get("category"),
                        "route_mode": args.mode,
                        "region": args.region,
                        "endpoint_host": endpoint_host,
                        "deployment_type": args.deployment_type,
                        "client_location": args.client_location,
                        "network_rtt_ms": rtt,
                        "tools_enabled": False,
                        "api": api,
                        "reasoning_effort": effort,
                        "arm": arm["arm_id"],
                        "model_requested": deployment,
                        "model_actually_served": served,
                        "served_model_family": billing_model,
                        "router_mode": routing.get("mode"),
                        "router_latency_ms": routing.get("router_latency_ms"),
                        "router_attempts": routing.get("attempts"),
                        "router_fallback": routing.get("fallback"),
                        "iteration": i,
                        "warmup": is_warmup,
                        "ttft_ms": m["ttft_ms"],
                        "e2e_ms": m["e2e_ms"],
                        "decode_ms": m["decode_ms"],
                        "tpot_ms": m["tpot_ms"],
                        "decode_tps": m["decode_tps"],
                        "answer_budget": answer_budget,
                        "max_output_tokens": cap,
                        "status": m["status"],
                        "truncated": m["truncated"],
                        "incomplete_reason": m["incomplete_reason"],
                        "finish_reason": m.get("finish_reason"),
                        "prompt_tokens": m["prompt_tokens"],
                        "cached_tokens": m["cached_tokens"],
                        "completion_tokens": m["completion_tokens"],
                        "reasoning_tokens": m["reasoning_tokens"],
                        "cost_usd": compute_cost(
                            pricing, billing_model or deployment,
                            m["prompt_tokens"], m["cached_tokens"], m["completion_tokens"],
                            registry),
                        "response_chars": len(m["response_text"]),
                        "response_text": m["response_text"],
                        "response_preview": m["response_text"][:200],
                        "error": m["error"],
                    }
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                    fh.flush()

                    tag = "WU" if is_warmup else "  "
                    if m["error"]:
                        print(f"  {tag} [{done}/{total}] {item['id']:5s} {arm['arm_id']:<26} ERROR {m['error'][:60]}")
                    else:
                        ttft = f"{m['ttft_ms']:.0f}ms" if m["ttft_ms"] is not None else "n/a"
                        tps = f"{m['decode_tps']:.0f}t/s" if m["decode_tps"] is not None else "n/a"
                        extra = f" served={billing_model}" if args.mode == "router" and served else ""
                        if routing.get("fallback"):
                            extra += " FALLBACK"
                        trunc = " TRUNC" if m["truncated"] else ""
                        print(f"  {tag} [{done}/{total}] {item['id']:5s} {arm['arm_id']:<26} "
                              f"TTFT={ttft:>7s} decode={tps:>8s} e2e={m['e2e_ms']:.0f}ms "
                              f"in={m['prompt_tokens']} out={m['completion_tokens']}"
                              f"(r={m['reasoning_tokens']}){extra}")

                    if args.sleep:
                        time.sleep(args.sleep)

    print(f"\nDone. {done} requests → {out_path}")
    return out_path


# --------------------------------------------------------------------------
# Preflight (no network, no credentials)
# --------------------------------------------------------------------------

def preflight(args) -> int:
    """Validate datasets, pricing, model registry and CLI wiring without any request."""
    print("=" * 68)
    print("  PREFLIGHT — no requests will be sent")
    print("=" * 68)

    dataset = load_jsonl(Path(args.dataset))
    print(f"\ndataset            {args.dataset}")
    print(f"records            {len(dataset)}")

    tiers: dict[str, int] = {}
    types: dict[str, int] = {}
    scenarios: dict[str, int] = {}
    partial: list[str] = []
    for item in dataset:
        if item.get("tier"):
            tiers[item["tier"]] = tiers.get(item["tier"], 0) + 1
        if item.get("task_type"):
            types[item["task_type"]] = types.get(item["task_type"], 0) + 1
        if item.get("scenario"):
            scenarios[item["scenario"]] = scenarios.get(item["scenario"], 0) + 1
        if item.get("scope") == "partial":
            partial.append(item["id"])
    if scenarios:
        print("Qira scenarios     " + ", ".join(f"{k}={v}" for k, v in sorted(scenarios.items())))
    if tiers:
        print("complexity tiers   " + ", ".join(f"{k}={v}" for k, v in sorted(tiers.items())))
    if types:
        print(f"task types         {len(types)} distinct")
    if partial:
        print(f"partial scope      {partial}")
        print("                   → text proxy only; the full path needs Voice Live (Live")
        print("                     Interaction) or the image benchmark (Creator Zone)")

    ids = [i["id"] for i in dataset]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        print(f"[FAIL] duplicate ids: {sorted(dupes)}")
        return 1
    print("id uniqueness      OK")

    targets = [d.strip() for d in args.deployments.split(",") if d.strip()]

    # --- fairness constraints -------------------------------------------------
    print("\n" + "-" * 68)
    print("  FAIRNESS CONSTRAINTS")
    print("-" * 68)
    print(f"  1. web search      DISABLED (no tools are ever attached)")
    print(f"  2. region          {args.region}")
    print(f"     deployment type {args.deployment_type}")
    print(f"     client location {args.client_location}")

    endpoint = os.environ.get("AZURE_OPENAI_ENDPOINT", "")
    host = urlparse(endpoint).netloc if endpoint else ""
    print(f"     endpoint host   {host or '(endpoint not set)'}")
    if host:
        rtt = measure_rtt(host)
        if rtt is None:
            print("     network RTT     could not measure")
        else:
            verdict = ("same-region" if rtt < 15 else
                       "regional" if rtt < 50 else "REMOTE — network will dominate TTFT")
            print(f"     network RTT     {rtt}ms median TCP connect → {verdict}")
            if rtt >= 50:
                print("     [warn] generate load from a VM in the same region;")
                print("            see scripts/provision-benchmark-vm.sh")
    print("     → all models below must be deployed on THIS endpoint/region;")
    print("       analyze.py refuses to aggregate results across regions.")

    registry = load_model_registry(Path(args.models))
    if args.efforts:
        print(f"  3. reasoning_effort EXPLICIT {args.efforts} for every deployment (--efforts)")
    else:
        print(f"  3. reasoning_effort {'FULL MATRIX' if args.matrix else 'pinned per model'}"
              f" from {Path(args.models).name}")
    print(f"     api surface     {'chat (forced by --mode router)' if args.mode == 'router' else args.api}")
    try:
        arms = build_arms(targets, registry, args.matrix, parse_efforts(args.efforts))
    except SystemExit as exc:
        print(f"     [FAIL] {exc}")
        return 1

    for t in targets:
        entry = registry.get(_normalize(t))
        if entry is None:
            print(f"     {t:<22} NOT IN REGISTRY → effort will not be sent")
            continue
        sup = entry.get("supported_efforts")
        probed = " (probed)" if entry.get("probed") else " (NOT probed — run probe_efforts.py)"
        print(f"     {t:<22} family={entry.get('family','?'):<14}"
              f" supported={sup if sup is not None else '?'}{probed}")

    print(f"\n  test arms ({len(arms)}):")
    for a in arms:
        print(f"     {a['arm_id']:<28} effort={a['effort'] or 'not sent'}")

    # --- pricing --------------------------------------------------------------
    pricing = load_pricing(Path(args.pricing))
    priced = [m for m, v in pricing.items()
              if v and all(v.get(k) is not None for k in ("input", "cached", "output"))]
    unpriced = [m for m in pricing if m not in priced]
    print(f"\npricing file       {args.pricing}")
    print(f"priced models      {priced or '(none)'}")
    if unpriced:
        print(f"UNPRICED           {unpriced}")
        print("                   → cost_usd will be null for these until filled in")

    missing_price = [t for t in targets
                     if compute_cost(pricing, t, 1000, 0, 100, registry) is None]
    if missing_price and args.mode == "direct":
        print(f"[warn] no confirmed pricing for target(s): {missing_price}")

    reqs = len(arms) * len(dataset) * (args.iterations + args.warmup)
    print(f"\nmode               {args.mode}")
    print(f"deployments        {targets}")
    print(f"iterations         {args.iterations} (+{args.warmup} warmup)")
    print(f"planned requests   {reqs}")

    have_ep = bool(os.environ.get("AZURE_OPENAI_ENDPOINT"))
    have_key = bool(os.environ.get("AZURE_OPENAI_API_KEY"))
    print(f"\nAZURE_OPENAI_ENDPOINT   {'set' if have_ep else 'NOT SET'}")
    print(f"AZURE_OPENAI_API_KEY    {'set' if have_key else 'NOT SET'}")

    if args.mode == "router":
        print("\n[Task B reminder] The first real router run must confirm that")
        print("model_actually_served differs from model_requested. If it echoes the")
        print("router deployment name instead, the router is not exposing its choice")
        print("and the documented fallback plan applies.")

    print("\nPreflight OK.")
    return 0


# --------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser(
        description="Unified harness for LLM benchmarking (Task A) and Model Router validation (Task B).")
    p.add_argument("--mode", choices=["direct", "router"], required=True,
                   help="direct = Task A candidate comparison; router = Task B routing validation")
    p.add_argument("--dataset", required=True, help="path to a .jsonl dataset")
    p.add_argument("--deployments", required=True,
                   help="comma-separated deployment names; for --mode router this is the router deployment")
    p.add_argument("--region", required=True,
                   help="Azure region label, e.g. eastus2. All compared models MUST be in this one region.")
    p.add_argument("--client-location", default="unknown", dest="client_location",
                   help="where the load is generated from, e.g. eastus2-linux-vm. Recorded for provenance.")
    p.add_argument("--deployment-type", default="paygo", choices=["paygo", "ptu"],
                   dest="deployment_type",
                   help="PAYGO GlobalStandard (default, matches AOAI-Model-Migration-Benchmark) or PTU")
    p.add_argument("--matrix", action="store_true",
                   help="expand each deployment across every supported reasoning_effort "
                        "(run probe_efforts.py --write first)")
    p.add_argument("--api", choices=["responses", "chat"], default="responses",
                   help="API surface. Task A used responses; --mode router forces chat because "
                        "router deployments reject the Responses API and only expose the routing "
                        "trace on Chat Completions")
    p.add_argument("--efforts", default=None,
                   help="comma-separated reasoning_effort values to send to EVERY deployment "
                        "(overrides the registry; use 'none' for the literal value none, "
                        "'-' for not sending the parameter). Task B uses this to test passthrough.")
    p.add_argument("--iterations", type=int, default=3, help="measured iterations per item (default 3)")
    p.add_argument("--warmup", type=int, default=1, help="warmup iterations per item, excluded from stats (default 1)")
    p.add_argument("--max-output-tokens", type=int, default=512,
                   dest="max_output_tokens", help="fallback cap when a dataset item does not set one")
    p.add_argument("--sleep", type=float, default=0.0, help="seconds to sleep between requests")
    p.add_argument("--pricing", default=str(DEFAULT_PRICING), help="path to pricing.json")
    p.add_argument("--models", default=str(DEFAULT_MODELS), help="path to models.json")
    p.add_argument("--preflight", action="store_true",
                   help="validate datasets/pricing/wiring and exit without sending requests")
    args = p.parse_args()

    if args.preflight:
        return preflight(args)

    run_benchmark(args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
