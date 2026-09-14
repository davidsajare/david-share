"""
Measurement and aggregation core for the live benchmark console.

This module deliberately owns no request-timing code of its own. It loads the
harness that produced the written studies in this folder tree and calls it, so
a number shown live on a projector and a number printed in the reports come
out of the same code path. If the harness is missing the console refuses to
start in live mode rather than silently measuring something else.

No tools and no web search are ever attached: the harness's request builders
send only a system message, optional prior turns and the user prompt, which is
the "native model capability" condition every study in this folder uses.
"""

from __future__ import annotations

import importlib.util
import json
import math
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PARENT = ROOT.parent

# The studies this console borrows its harness, registry and pricing from, in
# preference order. The readiness study is first because its harness is the
# newest (it adds multi-turn history and the usage-required guard).
STUDY_FOLDERS = (
    "qira-production-readiness",
    "qira-model-router-validation",
    "qira-followup-throughput-recalibration",
    "qira-scenario-model-benchmark",
)

SCENARIO_DATASET = "qira-scenario-model-benchmark/datasets/qira_scenarios.jsonl"
ROUTER_DATASET = "qira-model-router-validation/datasets/router_taskb.jsonl"

# The six Qira product scenarios the study measured, in the order the report
# presents them. Scenarios whose scope is "partial" are measured through a text
# proxy only; the note says what was not covered.
SCENARIO_ORDER = ("NextMove", "WriteForMe", "CatchMeUp", "PayAttention",
                  "LiveInteraction", "CreatorZone")
SCENARIO_NOTES = {
    "PayAttention": "Text side only: speech-to-text and the realtime voice path were not tested.",
    "LiveInteraction": "Text proxy for a realtime voice loop; a text score is not a voice-latency result.",
    "CreatorZone": "Prompt and edit-intent parsing only; image generation quality itself was not tested.",
}

# The three candidate models the written study compares. Everything else in the
# registry is a baseline, a router, or a deployment kept for another study.
STUDY_MODELS = ("gpt-4o-mini-bench", "gpt-5-mini", "gpt-5.6-luna")

# The study sends answer_budget + this headroom, so a reasoning model is not
# truncated while every arm still answers the same prompt under the same rules.
REASONING_HEADROOM = 8192
DEFAULT_MAX_OUTPUT_TOKENS = 8192
MAX_CONCURRENCY = 16
MAX_REQUESTS_PER_RUN = 400

# An answer whose first and last text delta are less than this apart did not
# stream: it arrived in one burst. The follow-up study fixed this threshold at
# 50 ms and showed that the ~10,000 tok/s "decode rates" seen on the Chat
# Completions path were entirely this artefact. Per-token pace is only a
# property of the model when the response actually streamed, so burst records
# are excluded from TPOT and decode-rate statistics and counted separately.
BURST_DELTA_MS = 50


class ConsoleError(RuntimeError):
    """Raised for conditions the operator can fix, and shown in the UI."""


# --------------------------------------------------------------------------
# Loading the study assets
# --------------------------------------------------------------------------

def study_root(name: str) -> Path:
    return PARENT / name


def find_harness() -> Path:
    for name in STUDY_FOLDERS:
        candidate = study_root(name) / "harness.py"
        if candidate.is_file():
            return candidate
    raise ConsoleError(
        "No study harness found next to the console. Expected one of: "
        + ", ".join(f"../{n}/harness.py" for n in STUDY_FOLDERS)
    )


_harness_lock = threading.Lock()
_harness_module = None


def load_harness():
    """Import the sibling study's harness.py once, by path."""
    global _harness_module
    with _harness_lock:
        if _harness_module is None:
            path = find_harness()
            spec = importlib.util.spec_from_file_location("qira_harness", path)
            if spec is None or spec.loader is None:
                raise ConsoleError(f"Could not load harness from {path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _harness_module = module
        return _harness_module


def _first_existing(relative: str) -> Path:
    for name in STUDY_FOLDERS:
        candidate = study_root(name) / relative
        if candidate.is_file():
            return candidate
    raise ConsoleError(f"Missing study asset: {relative}")


# Several sibling assets are stored in Git LFS. A clone made without git-lfs
# leaves a small text pointer in their place, and json.loads() then fails with
# a message that tells the operator nothing useful.
LFS_POINTER_PREFIX = "version https://git-lfs.github.com/spec/"


def read_json_asset(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    if text.startswith(LFS_POINTER_PREFIX):
        raise ConsoleError(
            f"{path.name} is an unfetched Git LFS pointer, not its contents. "
            "Run 'git lfs install && git lfs pull' in the repository, then start the console again."
        )
    return json.loads(text)


def load_pricing() -> dict:
    path = _first_existing("config/pricing.json")
    return read_json_asset(path).get("models", {})


def load_registry() -> dict:
    path = _first_existing("config/models.json")
    data = read_json_asset(path).get("models", {})
    return {k.strip().lower(): v for k, v in data.items()}


def load_scenarios() -> list[dict]:
    """
    Qira scenario prompts, with the two publicly withheld rows dropped.

    The withheld rows carry a marker instead of their prompt text (see the
    study folders' outputs/public_redaction.json). Sending a marker to a model
    would measure nothing, so they are not offered as live inputs.
    """
    path = PARENT / SCENARIO_DATASET
    if not path.is_file():
        raise ConsoleError(f"Scenario dataset not found at {path}")
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if "[withheld in the public copy" in row.get("text", ""):
            continue
        scenario = row.get("scenario") or "Other"
        rows.append({
            "id": row["id"],
            "scenario": scenario,
            "scenario_label": row.get("scenario_label") or scenario,
            "scope": row.get("scope"),
            "scope_note": SCENARIO_NOTES.get(scenario),
            "task_type": row.get("task_type"),
            "chars": len(row["text"]),
            "preview": row["text"][:180],
            "text": row["text"],
            # The study's per-prompt answer budget. The request cap is this
            # plus the uniform reasoning headroom.
            "answer_budget": row.get("max_output_tokens"),
        })
    rows.sort(key=lambda r: (SCENARIO_ORDER.index(r["scenario"])
                             if r["scenario"] in SCENARIO_ORDER else len(SCENARIO_ORDER),
                             r["id"]))
    return rows


def load_router_tiers() -> list[dict]:
    """
    Router difficulty-tiered prompts, used for the routing-behaviour view.

    Returns an empty list when the router study is not present, which keeps the
    console usable as a standalone folder.
    """
    path = PARENT / ROUTER_DATASET
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        row = json.loads(line)
        if "[withheld in the public copy" in row.get("text", ""):
            continue
        rows.append({
            "id": row["id"],
            "tier": row.get("tier"),
            "category": row.get("category"),
            "chars": len(row["text"]),
            "preview": row["text"][:180],
            "text": row["text"],
        })
    return rows


# --------------------------------------------------------------------------
# Catalog
# --------------------------------------------------------------------------

def load_deployment_facts() -> dict:
    """
    Deployment metadata captured by the studies when the resources were
    verified: region, SKU, capacity and rate limits.

    Two file shapes exist - a bare list, and a dict carrying the region - so
    both are accepted. The region matters: every arm must be served from one
    region, otherwise a latency comparison measures geography.
    """
    facts: dict[str, dict] = {}
    regions: set[str] = set()
    for name in STUDY_FOLDERS:
        folder = study_root(name)
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("outputs/deployment_verification*.json")):
            try:
                data = read_json_asset(path)
            except (ConsoleError, json.JSONDecodeError):
                continue
            if isinstance(data, dict):
                region = data.get("region")
                entries = data.get("deployments") or []
            else:
                region = None
                entries = data
            if region:
                regions.add(region)
            for entry in entries:
                deployment = (entry.get("deployment") or "").strip().lower()
                if not deployment:
                    continue
                facts.setdefault(deployment, {})
                facts[deployment].update({k: v for k, v in entry.items() if k != "deployment"})
                if region:
                    facts[deployment].setdefault("region", region)
    return {"deployments": facts, "regions": sorted(regions)}


def _is_router(entry: dict) -> bool:
    return (entry or {}).get("family") == "router"


def billing_key(deployment: str, registry: dict) -> str | None:
    entry = registry.get(deployment.strip().lower(), {}) or {}
    if _is_router(entry):
        return None
    return (entry.get("model_name") or deployment).strip().lower()


def catalog() -> dict:
    """Everything the UI needs to render its setup panel."""
    registry = load_registry()
    pricing = load_pricing()
    facts = load_deployment_facts()
    arms = []
    for name, entry in registry.items():
        entry = entry or {}
        key = billing_key(name, registry)
        price = pricing.get(key) if key else None
        fact = facts["deployments"].get(name, {})
        arms.append({
            "deployment": name,
            "family": entry.get("family"),
            "model_name": entry.get("model_name") or (None if _is_router(entry) else name),
            "is_router": _is_router(entry),
            "is_study_model": name in STUDY_MODELS,
            "routing_mode": entry.get("routing_mode"),
            "router_subset": entry.get("subset"),
            "default_effort": entry.get("reasoning_effort"),
            "supported_efforts": entry.get("supported_efforts") or [],
            "probed": bool(entry.get("probed")),
            "price_input": (price or {}).get("input"),
            "price_output": (price or {}).get("output"),
            "price_cached": (price or {}).get("cached"),
            "priced": price is not None,
            "region": fact.get("region"),
            "sku": fact.get("sku"),
            "capacity": fact.get("capacity"),
            "verified": bool(fact),
            # Model Router deployments reject the Responses API and only expose
            # their routing trace on Chat Completions.
            "api": "chat" if _is_router(entry) else "responses",
        })
    arms.sort(key=lambda a: (a["is_router"], not a["is_study_model"], a["deployment"]))
    return {
        "arms": arms,
        "scenarios": load_scenarios(),
        "router_tiers": load_router_tiers(),
        "study_models": list(STUDY_MODELS),
        "scenario_order": list(SCENARIO_ORDER),
        "regions": facts["regions"],
        "defaults": {
            "headroom": REASONING_HEADROOM,
            "max_output_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
            "iterations": 3,
            "concurrency": 1,
            "warmup": True,
        },
        "limits": {
            "max_concurrency": MAX_CONCURRENCY,
            "max_requests": MAX_REQUESTS_PER_RUN,
        },
    }


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------

def percentile(values: list[float], q: float):
    """Nearest-rank percentile, matching analyze.py in the study folders."""
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    ordered = sorted(clean)
    idx = max(0, min(len(ordered) - 1, math.ceil(q / 100 * len(ordered)) - 1))
    return ordered[idx]


def _mean(values: list[float]):
    clean = [v for v in values if v is not None]
    return statistics.mean(clean) if clean else None


def _r(value, digits=1):
    return None if value is None else round(value, digits)


def summarize_arm(arm: str, records: list[dict], wall_clock_s: float | None = None) -> dict:
    """
    Collapse one arm's measured records into the metrics the customer asked
    for: TTFT, TPOT, tokens per second, end-to-end latency at p50/p90, token
    composition and cost.

    Only records that both completed and carried usage are counted in the
    latency and cost statistics. Errors and truncations are reported alongside
    rather than being averaged in, because a failed call has no meaningful
    TTFT and silently dropping it would flatter the arm.

    Per-token statistics (TPOT, decode rate) are computed only over records
    that actually streamed. See BURST_DELTA_MS.
    """
    ok = [r for r in records if not r.get("error") and r.get("prompt_tokens")]
    errors = [r for r in records if r.get("error")]
    truncated = [r for r in ok if r.get("truncated")]

    burst = [r for r in ok if r.get("decode_ms") is not None and r["decode_ms"] < BURST_DELTA_MS]
    paced = [r for r in ok if r.get("decode_ms") is not None and r["decode_ms"] >= BURST_DELTA_MS]

    ttft = [r["ttft_ms"] for r in ok if r.get("ttft_ms") is not None]
    e2e = [r["e2e_ms"] for r in ok if r.get("e2e_ms") is not None]
    tpot = [r["tpot_ms"] for r in paced if r.get("tpot_ms") is not None]
    dtps = [r["decode_tps"] for r in paced if r.get("decode_tps") is not None]
    costs = [r["cost_usd"] for r in ok if r.get("cost_usd") is not None]

    emitted = [max((r.get("completion_tokens") or 0) - (r.get("reasoning_tokens") or 0), 0) for r in ok]
    total_output = sum(r.get("completion_tokens") or 0 for r in ok)
    total_emitted = sum(emitted)

    served = {}
    for r in ok:
        name = r.get("model_actually_served") or r.get("billing_model") or "unknown"
        served[name] = served.get(name, 0) + 1

    cost_mean = _mean(costs)
    summary = {
        "arm": arm,
        "n": len(records),
        "ok": len(ok),
        "errors": len(errors),
        "truncated": len(truncated),
        "bursts": len(burst),
        "burst_rate": round(len(burst) / len(ok), 3) if ok else None,
        "paced_n": len(paced),
        "error_samples": [r["error"] for r in errors[:3]],
        "ttft_p50_ms": _r(percentile(ttft, 50)),
        "ttft_p90_ms": _r(percentile(ttft, 90)),
        "e2e_p50_ms": _r(percentile(e2e, 50)),
        "e2e_p90_ms": _r(percentile(e2e, 90)),
        "tpot_p50_ms": _r(percentile(tpot, 50), 2),
        "tpot_p90_ms": _r(percentile(tpot, 90), 2),
        "decode_tps_p50": _r(percentile(dtps, 50), 2),
        "decode_tps_p90": _r(percentile(dtps, 90), 2),
        "prompt_tokens_mean": _r(_mean([r.get("prompt_tokens") for r in ok])),
        "reasoning_tokens_mean": _r(_mean([r.get("reasoning_tokens") for r in ok])),
        "emitted_tokens_mean": _r(_mean(emitted)),
        "output_tokens_mean": _r(_mean([r.get("completion_tokens") for r in ok])),
        "cost_usd_mean": None if cost_mean is None else round(cost_mean, 8),
        "cost_per_1k_requests": None if cost_mean is None else round(cost_mean * 1000, 4),
        # Totals for this arm as actually measured: what the run consumed and
        # what it would be billed, as opposed to the per-1,000 projection.
        "prompt_tokens_total": sum(r.get("prompt_tokens") or 0 for r in ok),
        "cached_tokens_total": sum(r.get("cached_tokens") or 0 for r in ok),
        "reasoning_tokens_total": sum(r.get("reasoning_tokens") or 0 for r in ok),
        "output_tokens_total": total_output,
        "total_tokens": sum((r.get("prompt_tokens") or 0) + (r.get("completion_tokens") or 0)
                            for r in ok),
        "cost_usd_total": round(sum(costs), 8) if costs else None,
        "served_split": served,
        "priced": cost_mean is not None,
    }

    # Cost per 1k emitted tokens answers "what does a unit of useful output
    # cost here", which is the comparison a buyer actually makes across models
    # whose verbosity differs.
    if costs and total_emitted > 0:
        summary["cost_per_1k_emitted_tokens"] = round(sum(costs) / total_emitted * 1000, 6)
    else:
        summary["cost_per_1k_emitted_tokens"] = None

    # Aggregate throughput only means something when the arm was driven with a
    # known wall clock; at concurrency 1 it degenerates to the decode rate.
    if wall_clock_s and wall_clock_s > 0 and total_output > 0:
        summary["run_output_tps"] = round(total_output / wall_clock_s, 2)
        summary["run_rps"] = round(len(ok) / wall_clock_s, 3)
    else:
        summary["run_output_tps"] = None
        summary["run_rps"] = None
    return summary


def value_score(summary: dict, baseline_cost: float | None) -> float | None:
    """
    Relative cost efficiency: how many times cheaper this arm is per 1k
    requests than the most expensive priced arm in the same run. Reported as a
    plain ratio so the chart needs no explanation on a projector.
    """
    cost = summary.get("cost_per_1k_requests")
    if not cost or not baseline_cost:
        return None
    return round(baseline_cost / cost, 2)


# --------------------------------------------------------------------------
# Run planning and execution
# --------------------------------------------------------------------------

@dataclass
class ArmSpec:
    deployment: str
    effort: str | None
    api: str

    @property
    def name(self) -> str:
        return f"{self.deployment}@{self.effort}" if self.effort else self.deployment


@dataclass
class RunPlan:
    arms: list[ArmSpec]
    items: list[dict]
    iterations: int
    concurrency: int
    max_output_tokens: int
    warmup: bool
    dataset: str = "scenarios"
    headroom: int | None = None

    def cap_for(self, item: dict) -> int:
        """
        The study sends each prompt's own answer budget plus a uniform
        reasoning headroom, so a reasoning model is never truncated while
        every arm answers the same prompt under the same rules. Prompts
        without a budget fall back to the flat cap.
        """
        if self.headroom is None:
            return self.max_output_tokens
        budget = item.get("answer_budget")
        if not budget:
            return self.max_output_tokens
        return int(budget) + int(self.headroom)

    @property
    def measured_calls(self) -> int:
        return len(self.arms) * len(self.items) * self.iterations

    @property
    def total_calls(self) -> int:
        extra = len(self.arms) * len(self.items) if self.warmup else 0
        return self.measured_calls + extra


def build_plan(request: dict, catalog_data: dict) -> RunPlan:
    """Validate a UI request and turn it into an executable plan."""
    registry = load_registry()
    by_id = {}
    dataset = request.get("dataset") or "scenarios"
    source = catalog_data["router_tiers"] if dataset == "router" else catalog_data["scenarios"]
    for row in source:
        by_id[row["id"]] = row

    item_ids = [i for i in (request.get("items") or []) if i in by_id]
    if not item_ids:
        raise ConsoleError("Select at least one prompt.")

    arms: list[ArmSpec] = []
    for raw in request.get("arms") or []:
        deployment = (raw.get("deployment") or "").strip()
        if not deployment:
            continue
        entry = registry.get(deployment.lower())
        if entry is None:
            raise ConsoleError(f"Unknown deployment: {deployment}")
        effort = raw.get("effort")
        if effort in ("", "default", None):
            effort = entry.get("reasoning_effort")
        if _is_router(entry):
            # Routers pick the served model themselves; forwarding an effort
            # would be sent to whichever model wins and break comparability.
            effort = None
        supported = entry.get("supported_efforts")
        if effort and supported and effort not in supported:
            raise ConsoleError(
                f"{deployment} does not support reasoning effort '{effort}'. "
                f"Supported: {', '.join(supported) or 'none'}."
            )
        api = "chat" if _is_router(entry) else (request.get("api") or "responses")
        arms.append(ArmSpec(deployment=deployment, effort=effort, api=api))

    if not arms:
        raise ConsoleError("Select at least one model or router deployment.")

    # Mixing a router (Chat Completions only) with direct arms on Responses
    # would attribute an API-surface difference to the router. The studies
    # solve this by putting every arm of such a run on Chat Completions.
    if any(a.api == "chat" for a in arms):
        for a in arms:
            a.api = "chat"

    iterations = max(1, min(int(request.get("iterations") or 3), 20))
    concurrency = max(1, min(int(request.get("concurrency") or 1), MAX_CONCURRENCY))
    max_output = max(64, min(int(request.get("max_output_tokens") or DEFAULT_MAX_OUTPUT_TOKENS), 32768))
    warmup = bool(request.get("warmup", True))
    headroom = request.get("headroom")
    if headroom is not None and headroom != "":
        headroom = max(0, min(int(headroom), 32768))
    else:
        headroom = None

    plan = RunPlan(
        arms=arms,
        items=[by_id[i] for i in item_ids],
        iterations=iterations,
        concurrency=concurrency,
        max_output_tokens=max_output,
        warmup=warmup,
        dataset=dataset,
        headroom=headroom,
    )
    if plan.total_calls > MAX_REQUESTS_PER_RUN:
        raise ConsoleError(
            f"That plan is {plan.total_calls} requests; the console caps a single run at "
            f"{MAX_REQUESTS_PER_RUN}. Reduce prompts, models or iterations."
        )
    return plan


@dataclass
class RunState:
    run_id: str
    plan: RunPlan
    cancel: threading.Event = field(default_factory=threading.Event)
    records: list[dict] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    arm_wall_clock: dict = field(default_factory=dict)


def execute_plan(plan: RunPlan, client, pricing: dict, registry: dict, harness,
                 emit, cancel: threading.Event) -> list[dict]:
    """
    Run every arm in turn, streaming one event per completed request.

    Arms run sequentially even when concurrency > 1 within an arm, because two
    arms sharing the same resource would contend for the same tokens-per-minute
    quota and neither number would be a property of the model.
    """
    all_records: list[dict] = []
    done = 0
    total = plan.total_calls

    for arm in plan.arms:
        if cancel.is_set():
            break
        arm_records: list[dict] = []
        emit("arm_start", {"arm": arm.name, "deployment": arm.deployment,
                           "effort": arm.effort, "api": arm.api})

        # Warm-up pass: one call per prompt, measured but not scored. A cold
        # deployment's first call carries connection setup and scheduler
        # placement that no steady-state user ever sees.
        passes = []
        if plan.warmup:
            passes.append((0, True))
        passes.extend((i + 1, False) for i in range(plan.iterations))

        measured_wall = 0.0
        for iteration, is_warmup in passes:
            if cancel.is_set():
                break
            batch = [(item, iteration, is_warmup) for item in plan.items]
            pass_started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=plan.concurrency) as pool:
                futures = [
                    pool.submit(_one_call, harness, client, arm, item, plan,
                                pricing, registry, iteration, is_warmup, cancel)
                    for item, iteration, is_warmup in batch
                ]
                for future in futures:
                    record = future.result()
                    done += 1
                    if record is None:
                        continue
                    if not record["warmup"]:
                        arm_records.append(record)
                        all_records.append(record)
                    emit("record", {**record, "progress": round(done / total, 4),
                                    "done": done, "total": total})
            if not is_warmup:
                measured_wall += time.perf_counter() - pass_started
        emit("arm_summary", summarize_arm(
            arm.name, arm_records, measured_wall if plan.concurrency > 1 else None))

    return all_records


def _one_call(harness, client, arm: ArmSpec, item: dict, plan: RunPlan,
              pricing: dict, registry: dict, iteration: int, is_warmup: bool,
              cancel: threading.Event):
    if cancel.is_set():
        return None
    started = time.time()
    cap = plan.cap_for(item)
    result = harness.run_one(
        client,
        arm.deployment,
        {"id": item["id"], "text": item["text"]},
        cap,
        arm.effort,
        api=arm.api,
    )
    billing = (result.get("model_actually_served")
               or billing_key(arm.deployment, registry)
               or arm.deployment)
    normalized = harness.billing_model_name(billing) or billing
    cost = harness.compute_cost(
        pricing, normalized,
        result.get("prompt_tokens") or 0,
        result.get("cached_tokens") or 0,
        result.get("completion_tokens") or 0,
        registry,
    )
    routing = result.get("routing") or {}
    return {
        **{k: v for k, v in result.items() if k != "response_text"},
        "arm": arm.name,
        "deployment": arm.deployment,
        "effort": arm.effort,
        "item_id": item["id"],
        "scenario": item.get("scenario") or item.get("tier"),
        "answer_budget": item.get("answer_budget"),
        "max_output_tokens": cap,
        "iteration": iteration,
        "warmup": is_warmup,
        "billing_model": normalized,
        "cost_usd": cost,
        "started_at": started,
        "preview": (result.get("response_text") or "")[:220],
        "routing_mode": routing.get("mode") if isinstance(routing, dict) else None,
    }
