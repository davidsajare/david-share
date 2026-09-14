"""Validate the production-readiness runs and generate CSVs plus both READMEs.

Inputs (retained under outputs/):
  raw_fulltext/sessions_<run>.jsonl          multi-turn Qira sessions, 5 arms x 6 sessions x 3 iterations x 4 turns
  raw_fulltext/sustained_<run>.jsonl + sustained_<run>.summary.json
                                             fixed-duration continuous-refill load, 4 arms x {4,8,16}
  raw_fulltext/resilience_<run>.jsonl + resilience_<run>.summary.json
                                             capacity-10 primary driven past its limit under none/reactive/proactive
  model_lifecycle_swedencentral.json         Models API lifecycle/capability facts (scripts/query_model_lifecycle.py)

Every aggregate is recomputed from the raw records; disagreement with a runner's live
summary raises. A table is not produced from a run that cannot prove its matrix.

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import argparse
import ast
import csv
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import analyze  # noqa: E402

OUTPUT = ROOT / "outputs"
RAW = OUTPUT / "raw_fulltext"
SESSION_ARMS = ("gpt-4o-mini-bench", "gpt-5-mini@minimal", "gpt-5.6-luna@none", "router-sol-luna-balanced", "router-sol-luna-quality")
LOAD_ARMS = ("gpt-4o-mini-bench", "gpt-5-mini@minimal", "gpt-5.6-luna@none", "router-sol-luna-balanced")
LEVELS = (4, 8, 16)
POLICIES = ("none", "reactive", "proactive")
SESSIONS, ITERATIONS, TURNS = 6, 3, 4


def pct(values, q):
    return analyze.pct(values, q)


def load_jsonl(path: Path):
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_run(prefix: str) -> str:
    runs = sorted({p.name[len(prefix) + 1:].split(".")[0] for p in RAW.glob(f"{prefix}_*.jsonl")})
    if len(runs) != 1:
        raise ValueError(f"Expected exactly one {prefix} run under outputs/raw_fulltext, found {runs}")
    return runs[0]


def raw_path(prefix: str, run: str) -> Path:
    """Sessions and resilience keep full answers; the sustained run is retained numerically (see README §6)."""
    full = RAW / f"{prefix}_{run}.jsonl"
    return full if full.exists() else RAW / f"{prefix}_{run}.numeric.jsonl"


def valid(r):
    return not r.get("error") and r.get("status") == "completed" and not r.get("truncated") and r["prompt_tokens"] > 0


def require_environment(rows, label):
    envs = {(r["region"], r["endpoint_host"], r["client_location"]) for r in rows}
    if len(envs) != 1 or next(iter(envs))[0] != "swedencentral":
        raise ValueError(f"{label}: mixed or unexpected environment {envs}")
    if any(r["tools_enabled"] is not False for r in rows):
        raise ValueError(f"{label}: a record had tools enabled")


# ------------------------------------------------------------------ 1. sessions
def sessions_report():
    run = find_run("sessions")
    rows = load_jsonl(RAW / f"sessions_{run}.jsonl")
    require_environment(rows, "sessions")
    expected = len(SESSION_ARMS) * SESSIONS * ITERATIONS * TURNS
    if len(rows) != expected:
        raise ValueError(f"sessions: {len(rows)} turns, expected {expected} (a broken turn aborts its session replay)")
    keys = {(r["arm"], r["session_id"], r["iteration"], r["turn"]) for r in rows}
    if len(keys) != expected:
        raise ValueError("sessions: duplicate turn cells")
    bad = [r for r in rows if not valid(r)]
    if bad:
        raise ValueError(f"sessions: {len(bad)} invalid turns, e.g. {[(r['arm'], r['session_id'], r['turn'], r['error']) for r in bad[:3]]}")
    for r in rows:
        if r["history_messages"] != 2 * (r["turn"] - 1):
            raise ValueError(f"sessions: history length wrong at {r['arm']}/{r['session_id']}/T{r['turn']}")
        if r["arm"].startswith("router-") and (not r["router_mode"] or not r["served_model_family"]):
            raise ValueError("sessions: router turn without routing trace")

    by_turn, by_arm, sequences = [], [], []
    for arm in SESSION_ARMS:
        g = [r for r in rows if r["arm"] == arm]
        for turn in range(1, TURNS + 1):
            t = [r for r in g if r["turn"] == turn]
            by_turn.append({
                "arm": arm, "turn": turn, "n": len(t),
                "prompt_tokens_mean": round(statistics.mean(r["prompt_tokens"] for r in t), 1),
                "cached_tokens_mean": round(statistics.mean(r["cached_tokens"] for r in t), 1),
                "turns_with_cache_hit": sum(1 for r in t if r["cached_tokens"] > 0),
                "cached_share_of_prompt": round(sum(r["cached_tokens"] for r in t) / sum(r["prompt_tokens"] for r in t), 4),
                "output_tokens_mean": round(statistics.mean(r["completion_tokens"] for r in t), 1),
                "reasoning_tokens_mean": round(statistics.mean(r["reasoning_tokens"] for r in t), 1),
                "ttft_p50_ms": pct([r["ttft_ms"] for r in t], 50), "ttft_p90_ms": pct([r["ttft_ms"] for r in t], 90),
                "e2e_p50_ms": pct([r["e2e_ms"] for r in t], 50),
                "cost_usd_mean": round(statistics.mean(r["cost_usd"] for r in t), 7),
                "sol_share": round(sum(1 for r in t if r["served_model_family"] == "gpt-5.6-sol") / len(t), 3) if arm.startswith("router-") else None,
            })
        # per completed session (arm, session, iteration): totals over the 4 turns
        totals = defaultdict(lambda: {"cost": 0.0, "prompt": 0, "completion": 0, "ttft_sum": 0.0, "e2e_sum": 0.0, "families": []})
        for r in sorted(g, key=lambda r: (r["session_id"], r["iteration"], r["turn"])):
            s = totals[(r["session_id"], r["iteration"])]
            s["cost"] += r["cost_usd"]; s["prompt"] += r["prompt_tokens"]; s["completion"] += r["completion_tokens"]
            s["ttft_sum"] += r["ttft_ms"]; s["e2e_sum"] += r["e2e_ms"]
            if r["served_model_family"]:
                s["families"].append(r["served_model_family"])
        first_turn_cost = statistics.mean(r["cost_usd"] for r in g if r["turn"] == 1)
        # Prompt caching needs a ≥1,024-token prefix that an earlier request already sent. Turn k's prompt
        # is a prefix of turn k+1's, so the longest reusable prefix in a session is max(prompt tokens of turns 1..3).
        reusable_prefix = max(max(r["prompt_tokens"] for r in g if (r["session_id"], r["iteration"]) == key and r["turn"] < TURNS) for key in totals)
        by_arm.append({
            "arm": arm, "sessions": len(totals), "turns": len(g),
            "session_cost_usd_mean": round(statistics.mean(s["cost"] for s in totals.values()), 6),
            "session_cost_usd_per_1000_sessions": round(statistics.mean(s["cost"] for s in totals.values()) * 1000, 3),
            "session_prompt_tokens_mean": round(statistics.mean(s["prompt"] for s in totals.values()), 1),
            "session_completion_tokens_mean": round(statistics.mean(s["completion"] for s in totals.values()), 1),
            "turn1_cost_x4_usd": round(first_turn_cost * 4, 6),
            "session_cost_vs_4x_turn1": round(statistics.mean(s["cost"] for s in totals.values()) / (first_turn_cost * 4), 3),
            "session_user_wait_ttft_sum_p50_ms": pct([s["ttft_sum"] for s in totals.values()], 50),
            "session_e2e_sum_p50_ms": pct([s["e2e_sum"] for s in totals.values()], 50),
            "turns_with_cache_hit": sum(1 for r in g if r["cached_tokens"] > 0),
            "max_prompt_tokens_seen": max(r["prompt_tokens"] for r in g),
            "max_reusable_prefix_tokens": reusable_prefix,
            "sessions_with_model_change": sum(1 for s in totals.values() if len(set(s["families"])) > 1) if arm.startswith("router-") else None,
            "sol_turn_share": round(sum(1 for r in g if r["served_model_family"] == "gpt-5.6-sol") / len(g), 3) if arm.startswith("router-") else None,
        })
        if arm.startswith("router-"):
            for (sid, it), s in sorted(totals.items()):
                sequences.append({"arm": arm, "session_id": sid, "iteration": it,
                                  "served_sequence": ">".join(f.replace("gpt-5.6-", "") for f in s["families"]),
                                  "model_changed_within_session": len(set(s["families"])) > 1})
    write_csv(OUTPUT / "sessions_by_turn.csv", by_turn)
    write_csv(OUTPUT / "sessions_by_arm.csv", by_arm)
    write_csv(OUTPUT / "sessions_router_sequences.csv", sequences)
    return run, rows, by_turn, by_arm, sequences


def router_429_traces(records):
    """Parse the routing trace embedded in the router deployment's own 429 payloads.

    The SDK error text is `RateLimitError: Error code: 429 - {python-repr dict}`; the dict carries
    model_selection_details.model_router_details.routing_trace. Returns per-request attempt lists.
    Raises if any 429 payload lacks a parseable trace, so the README sentence cannot silently drift.
    """
    out = []
    for r in records:
        text = r["error"] or ""
        start = text.find("{")
        if start < 0:
            raise ValueError("router 429 without a payload")
        body = ast.literal_eval(text[start:])
        trace = ((body.get("model_selection_details") or {}).get("model_router_details") or {}).get("routing_trace") or []
        attempts = [a for hop in trace for a in hop.get("attempts", [])]
        if not attempts:
            raise ValueError("router 429 payload without routing attempts")
        out.append({"attempts": attempts,
                    "retry_after_s": next((float(v) for a in attempts for k, v in ((a.get("result") or {}).get("headers") or {}).items() if k.lower() == "retry-after"), None),
                    "limit_requests": next((int(v) for a in attempts for k, v in ((a.get("result") or {}).get("headers") or {}).items() if k.lower() == "x-ratelimit-limit-requests"), None)})
    return out


# ------------------------------------------------------------------ 2. sustained load
def sustained_report():
    run = find_run("sustained")
    rows = load_jsonl(raw_path("sustained", run))
    summary = json.loads((OUTPUT / f"sustained_{run}.summary.json").read_text(encoding="utf-8"))
    require_environment(rows, "sustained")
    out = []
    router_429 = {"requests": 0, "attempts_histogram": {}, "models_tried": {}, "retry_after_s": [], "limit_requests": set()}
    for arm in LOAD_ARMS:
        for level in LEVELS:
            g = [r for r in rows if r["arm"] == arm and r["level"] == level]
            if not g:
                raise ValueError(f"sustained: no records for {arm} N={level}")
            live = next((s for s in summary["summaries"] if s["arm"] == arm and s["level"] == level), None)
            if live is None:
                raise ValueError(f"sustained: summary missing {arm} N={level}")
            counted = [r for r in g if r["counted"]]
            ok = [r for r in counted if valid(r)]
            errors = [r for r in counted if r["error"]]
            window = summary["duration_s"] - summary["ramp_s"]
            if live["counted"] != len(counted) or live["ok"] != len(ok) or live["errors"] != len(errors) or live["all_records"] != len(g):
                raise ValueError(f"sustained: live summary disagrees with records for {arm} N={level}")
            if arm.startswith("router-"):
                for t in router_429_traces([r for r in g if r["error_class"] == "429"]):
                    router_429["requests"] += 1
                    router_429["attempts_histogram"][len(t["attempts"])] = router_429["attempts_histogram"].get(len(t["attempts"]), 0) + 1
                    for a in t["attempts"]:
                        key = f"{a.get('model')}→{(a.get('result') or {}).get('status')}"
                        router_429["models_tried"][key] = router_429["models_tried"].get(key, 0) + 1
                    if t["retry_after_s"] is not None:
                        router_429["retry_after_s"].append(t["retry_after_s"])
                    if t["limit_requests"] is not None:
                        router_429["limit_requests"].add(t["limit_requests"])
            # in-flight profile inside the counting window, from offsets
            ramp_ms, deadline_ms = summary["ramp_s"] * 1000, summary["duration_s"] * 1000
            events = sorted([(max(r["started_offset_ms"], ramp_ms), 1) for r in g if r["finished_offset_ms"] > ramp_ms] +
                            [(min(r["finished_offset_ms"], deadline_ms), -1) for r in g if r["finished_offset_ms"] > ramp_ms])
            in_flight, prev, at_target, peak = 0, ramp_ms, 0.0, 0
            for ts, d in events:
                if in_flight >= level:
                    at_target += ts - prev
                in_flight += d; peak = max(peak, in_flight); prev = ts
            if peak > level:
                raise ValueError(f"sustained: peak in-flight {peak} exceeds level {level}")
            out_tokens = sum(r["completion_tokens"] for r in ok)
            ttft = [r["ttft_ms"] for r in ok if r["ttft_ms"] is not None]
            out.append({
                "arm": arm, "api": g[0]["api"], "level": level, "window_s": window, "counted": len(counted), "ok": len(ok),
                "errors": len(errors), "errors_429": sum(1 for r in errors if r["error_class"] == "429"),
                "other_errors": sum(1 for r in errors if r["error_class"] != "429"), "truncated": sum(1 for r in counted if r["truncated"]),
                "cut_by_deadline": sum(1 for r in g if r["cut_by_deadline"]), "peak_in_flight": peak,
                "share_of_window_at_target": round(at_target / (deadline_ms - ramp_ms), 3),
                "requests_per_s": round(len(ok) / window, 3),
                "aggregate_output_tok_per_s": round(out_tokens / window, 1),
                "aggregate_visible_tok_per_s": round(sum(r["completion_tokens"] - r["reasoning_tokens"] for r in ok) / window, 1),
                "reasoning_share_of_output": round(sum(r["reasoning_tokens"] for r in ok) / out_tokens, 3) if out_tokens else None,
                "ttft_p50_ms": pct(ttft, 50), "ttft_p90_ms": pct(ttft, 90), "ttft_p95_ms": pct(ttft, 95),
                "e2e_p50_ms": pct([r["e2e_ms"] for r in ok], 50), "e2e_p90_ms": pct([r["e2e_ms"] for r in ok], 90),
                "e2e_p95_ms": pct([r["e2e_ms"] for r in ok], 95),
                "output_tokens_mean": round(statistics.mean(r["completion_tokens"] for r in ok), 1) if ok else None,
                "cost_usd_sum": round(sum(r["cost_usd"] or 0 for r in ok), 6),
                "served_families": "|".join(sorted({r["served_model_family"] for r in ok if r["served_model_family"]})),
            })
    write_csv(OUTPUT / "sustained_levels.csv", out)
    router_429["limit_requests"] = sorted(router_429["limit_requests"])
    summary["router_429_trace"] = router_429
    return run, rows, summary, out


# ------------------------------------------------------------------ 3. resilience
def resilience_report():
    run = find_run("resilience")
    rows = load_jsonl(RAW / f"resilience_{run}.jsonl")
    summary = json.loads((OUTPUT / f"resilience_{run}.summary.json").read_text(encoding="utf-8"))
    require_environment(rows, "resilience")
    chain = summary["chain"]
    out = []
    for policy in POLICIES:
        g = [r for r in rows if r["policy"] == policy]
        live = next((s for s in summary["summaries"] if s["policy"] == policy), None)
        if not g or live is None:
            raise ValueError(f"resilience: missing policy {policy}")
        counted = [r for r in g if r["counted"]]
        ok = [r for r in counted if valid(r)]
        errors = [r for r in counted if r["error"]]
        if live["counted"] != len(counted) or live["ok"] != len(ok) or live["errors"] != len(errors) or live["all_records"] != len(g):
            raise ValueError(f"resilience: live summary disagrees with records for {policy}")
        if policy == "none" and any(r["served_by_fallback"] for r in g):
            raise ValueError("resilience: policy none must never be served by the fallback")
        for r in ok:
            if r["served_by_fallback"] and r["served_by_deployment"] != chain[1]:
                raise ValueError("resilience: fallback-served record names an unexpected deployment")
        window = summary["duration_s"] - summary["ramp_s"]
        by_fb = [r for r in ok if r["served_by_fallback"]]
        by_pri = [r for r in ok if not r["served_by_fallback"]]
        retry_after = [r["primary_retry_after_s"] for r in counted if r["primary_retry_after_s"] is not None]
        out.append({
            "policy": policy, "primary": chain[0], "fallback": "/".join(chain[1:]), "level": summary["level"], "window_s": window,
            "counted": len(counted), "ok": len(ok), "errors": len(errors), "errors_429": sum(1 for r in errors if r["error_class"] == "429"),
            "other_errors": sum(1 for r in errors if r["error_class"] != "429"),
            "success_rate": round(len(ok) / len(counted), 4) if counted else None,
            "served_by_primary": len(by_pri), "served_by_fallback": len(by_fb),
            "fallback_share_of_ok": round(len(by_fb) / len(ok), 4) if ok else None,
            "proactive_skips": sum(1 for r in counted if r["proactive_skip"]),
            "primary_rate_limited": sum(1 for r in counted if r.get("primary_rate_limited")),
            "primary_limit_seen_at_request_time": sum(1 for r in counted if r.get("primary_failure_phase") == "request"),
            "primary_limit_seen_in_stream": sum(1 for r in counted if r.get("primary_failure_phase") == "stream"),
            "in_stream_fallbacks": sum(r.get("in_stream_fallbacks") or 0 for r in counted),
            "primary_http_429": sum(1 for r in counted for a in (r["attempts"] or []) if a.get("position") == 0 and a.get("status") == 429),
            "retry_after_s_p50": pct(retry_after, 50), "retry_after_s_max": max(retry_after) if retry_after else None,
            "requests_per_s": round(len(ok) / window, 3), "aggregate_output_tok_per_s": round(sum(r["completion_tokens"] for r in ok) / window, 1),
            "ttft_p50_ms": pct([r["ttft_ms"] for r in ok], 50), "ttft_p90_ms": pct([r["ttft_ms"] for r in ok], 90),
            "ttft_primary_p50_ms": pct([r["ttft_ms"] for r in by_pri], 50), "ttft_fallback_p50_ms": pct([r["ttft_ms"] for r in by_fb], 50),
            "ttft_fallback_net_of_overhead_p50_ms": pct([r["ttft_ms"] - (r["fallback_overhead_ms"] or 0) for r in by_fb], 50),
            "in_stream_limits_all_records": sum(1 for r in g if r.get("primary_failure_phase") == "stream"),
            "request_time_limits_all_records": sum(1 for r in g if r.get("primary_failure_phase") == "request"),
            "all_records": len(g),
            "fallback_overhead_ms_p50": pct([r["fallback_overhead_ms"] for r in by_fb if r["fallback_overhead_ms"]], 50),
            "fallback_overhead_ms_p90": pct([r["fallback_overhead_ms"] for r in by_fb if r["fallback_overhead_ms"]], 90),
            "e2e_p50_ms": pct([r["e2e_ms"] for r in ok], 50),
            "cost_usd_sum": round(sum(r["cost_usd"] or 0 for r in ok), 6),
        })
    write_csv(OUTPUT / "resilience_policies.csv", out)
    return run, rows, summary, out


# ------------------------------------------------------------------ 4. lifecycle
def lifecycle_table():
    data = json.loads((OUTPUT / "model_lifecycle_swedencentral.json").read_text(encoding="utf-8"))
    return data


# ------------------------------------------------------------------ README
def t(en, zh):
    return {"en": en, "zh": zh}


def render(lang, blocks):
    return "\n".join(b[lang] if isinstance(b, dict) else b for b in blocks).rstrip("\n") + "\n"


def table(header_en, header_zh, rows):
    def build(h):
        cols = h.split("|")
        lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---:" if i else "---" for i in range(len(cols))) + "|"]
        lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
        return "\n".join(lines)
    return {"en": build(header_en), "zh": build(header_zh)}


def s(v):  # ms -> s string
    return "-" if v is None else f"{v / 1000:.2f}"


def build_readmes(run_s, by_turn, by_arm, sequences, run_l, load_rows, load_summary, run_r, res_rows, res_summary, lifecycle):
    arm_of = {a["arm"]: a for a in by_arm}
    turn_of = {(r["arm"], r["turn"]): r for r in by_turn}
    sess_tbl = [[a["arm"], f"{turn_of[(a['arm'], 1)]['prompt_tokens_mean']:.0f} → {turn_of[(a['arm'], 4)]['prompt_tokens_mean']:.0f}",
                 f"{a['turns_with_cache_hit']}/{a['turns']}", f"{s(turn_of[(a['arm'], 1)]['ttft_p50_ms'])} → {s(turn_of[(a['arm'], 4)]['ttft_p50_ms'])}",
                 s(a["session_user_wait_ttft_sum_p50_ms"]), f"{a['session_cost_usd_per_1000_sessions']:.3f}", f"{a['session_cost_vs_4x_turn1']:.2f}×",
                 (f"{a['sol_turn_share']:.0%} / {a['sessions_with_model_change']}" if a["sol_turn_share"] is not None else "—")] for a in by_arm]
    cache_hits_total = sum(a["turns_with_cache_hit"] for a in by_arm)
    max_prompt = max(a["max_prompt_tokens_seen"] for a in by_arm)
    max_reusable = max(a["max_reusable_prefix_tokens"] for a in by_arm)
    router_changes = {a["arm"]: a["sessions_with_model_change"] for a in by_arm if a["sessions_with_model_change"] is not None}
    quality_seqs = sorted({q["served_sequence"] for q in sequences if q["arm"] == "router-sol-luna-quality"})
    quality_sol_by_turn = [turn_of[("router-sol-luna-quality", k)]["sol_share"] for k in range(1, TURNS + 1)]
    ratio_lo, ratio_hi = min(a["session_cost_vs_4x_turn1"] for a in by_arm), max(a["session_cost_vs_4x_turn1"] for a in by_arm)
    cheapest, dearest = min(by_arm, key=lambda a: a["session_cost_usd_per_1000_sessions"]), max(by_arm, key=lambda a: a["session_cost_usd_per_1000_sessions"])

    load_tbl = [[r["arm"], r["level"], f"{r['ok']}/{r['counted']}", r["errors_429"], f"{r['share_of_window_at_target']:.0%}", f"{r['requests_per_s']:.2f}",
                 f"{r['aggregate_output_tok_per_s']:.0f}", f"{r['aggregate_visible_tok_per_s']:.0f}", f"{s(r['ttft_p50_ms'])} / {s(r['ttft_p90_ms'])} / {s(r['ttft_p95_ms'])}",
                 f"{s(r['e2e_p50_ms'])} / {s(r['e2e_p90_ms'])}"] for r in load_rows]
    total_429 = sum(r["errors_429"] for r in load_rows)
    total_other = sum(r["other_errors"] for r in load_rows)
    router_l = {r["level"]: r for r in load_rows if r["arm"] == "router-sol-luna-balanced"}
    min_share = min(r["share_of_window_at_target"] for r in load_rows)

    def lvl(arm, level, key):
        return next(r for r in load_rows if r["arm"] == arm and r["level"] == level)[key]

    scale_tbl = [[arm, f"{lvl(arm, 4, 'aggregate_visible_tok_per_s'):.0f} → {lvl(arm, 16, 'aggregate_visible_tok_per_s'):.0f}",
                  f"{lvl(arm, 4, 'requests_per_s'):.2f} → {lvl(arm, 16, 'requests_per_s'):.2f}",
                  f"{s(lvl(arm, 4, 'ttft_p50_ms'))} → {s(lvl(arm, 16, 'ttft_p50_ms'))}", f"{s(lvl(arm, 4, 'ttft_p90_ms'))} → {s(lvl(arm, 16, 'ttft_p90_ms'))}",
                  f"{lvl(arm, 4, 'errors_429')} / {lvl(arm, 8, 'errors_429')} / {lvl(arm, 16, 'errors_429')}"] for arm in LOAD_ARMS]

    res_of = {r["policy"]: r for r in res_rows}
    res_tbl = [[r["policy"], f"{r['ok']}/{r['counted']}", f"{r['success_rate']:.1%}", r["errors_429"], r["served_by_primary"], r["served_by_fallback"],
                r["in_stream_fallbacks"], r["proactive_skips"], s(r["ttft_primary_p50_ms"]), s(r["ttft_fallback_p50_ms"]),
                f"{r['fallback_overhead_ms_p50']:.0f} / {r['fallback_overhead_ms_p90']:.0f}" if r["fallback_overhead_ms_p50"] is not None else "-",
                f"{r['requests_per_s']:.2f}"] for r in res_rows]
    chain = res_summary["chain"]
    none_r, react_r, pro_r = res_of["none"], res_of["reactive"], res_of["proactive"]
    limit_total = sum(r["primary_rate_limited"] for r in res_rows)
    limit_request_time = sum(r["primary_limit_seen_at_request_time"] for r in res_rows)
    limit_in_stream = sum(r["primary_limit_seen_in_stream"] for r in res_rows)
    http429 = sum(r["primary_http_429"] for r in res_rows)
    limit_all_records = sum(r["in_stream_limits_all_records"] for r in res_rows)
    request_time_all_records = sum(r["request_time_limits_all_records"] for r in res_rows)
    rt = load_summary["router_429_trace"]
    router_undisturbed_ttft = next(r for r in load_rows if r["arm"] == "gpt-5.6-luna@none" and r["level"] == 8)["ttft_p50_ms"]

    life_tbl = [[m["model"], m["version"], m["lifecycle_status_docs_meaning"], str(m["inference_retirement_utc"])[:10],
                 ", ".join(k for k, v in m["capabilities"].items() if v == "true" and k in ("chatCompletion", "responses", "assistants", "jsonObjectResponse", "fineTune")),
                 ", ".join(x for x in m["skus"] if "Standard" in x or "Provisioned" in x), m["role_in_studies"]] for m in lifecycle["models"]]
    life_of = {m["model"]: m for m in lifecycle["models"]}

    blocks = [
        t("# Lenovo Qira — Production readiness: sessions, sustained load, fallback, lifecycle",
          "# Lenovo Qira — 生产就绪测试：多轮会话、持续负载、故障回退、生命周期"),
        "",
        t("The three earlier studies ([scenario benchmark](../qira-scenario-model-benchmark/README.md), [router validation](../qira-model-router-validation/README.md), [follow-up](../qira-followup-throughput-recalibration/README.md)) measured single turns at concurrency 1 and said so. This one closes the four gaps a production decision still leaves open: **multi-turn session cost and latency**, **steady-state throughput under sustained concurrency**, **behaviour at the PAYGO rate limit with and without a fallback**, and **model lifecycle / API compatibility**. Same Sweden Central VM and resource, no search or tools, hardened harness (`max_retries=0`, usage required).",
          "前三份研究（[场景基准](../qira-scenario-model-benchmark/README-CN.md)、[Router 验证](../qira-model-router-validation/README-CN.md)、[跟进实验](../qira-followup-throughput-recalibration/README-CN.md)）测的都是并发 1 的单轮请求，并已如实说明。本轮补上做生产决策还缺的四项：**多轮会话的成本与时延**、**持续并发下的稳态吞吐**、**触及 PAYGO 限流时有无回退的表现**、以及**模型生命周期与 API 兼容性**。同一台 Sweden Central VM、同一资源、不带搜索或工具，使用加固后的 harness（`max_retries=0`、必须收到 usage）。"),
        "",
        t(f"> Author: **Xinyu Wei (魏新宇)** · 2026-09-11 · runs `sessions_{run_s}`, `sustained_{run_l}`, `resilience_{run_r}`; lifecycle queried {lifecycle['checked_at_utc'][:10]}",
          f"> 作者: **Xinyu Wei (魏新宇)** · 2026-09-11 · 运行 `sessions_{run_s}`、`sustained_{run_l}`、`resilience_{run_r}`；生命周期查询于 {lifecycle['checked_at_utc'][:10]}"),
        "",
        "[English](README.md) | [中文](README-CN.md)",
        "",
        t("## Decision summary", "## 决策摘要"),
        "",
        t(f"- **Budget Qira per conversation, not per turn — and the two are not far apart on these scripts.** A 4-turn session cost {ratio_lo:.2f}–{ratio_hi:.2f}× four first turns: re-sending the history grew prompt tokens from turn 1 to turn 4 on every arm (table below), but the scripted follow-up turns are capped shorter than the opener (400 → 200 → 200 → 80 output tokens in `datasets/qira_sessions.jsonl`, a design choice), so the two effects roughly cancel here. Per 1,000 sessions: {cheapest['arm']} ${cheapest['session_cost_usd_per_1000_sessions']:.2f} … {dearest['arm']} ${dearest['session_cost_usd_per_1000_sessions']:.2f}. **Prompt caching contributed nothing: {cache_hits_total}/{sum(a['turns'] for a in by_arm)} turns reported cached tokens.** Caching needs a ≥ 1,024-token prefix that an earlier request already sent; the longest prompt that was later re-sent as a prefix was {max_reusable} tokens. Longer conversations or long system prompts would cross that line — not measured here.",
          f"- **Qira 的预算要按会话算，不按单轮算——在这些脚本上两者相差不大。**一个 4 轮会话的成本是 4 个首轮的 {ratio_lo:.2f}–{ratio_hi:.2f} 倍：重发历史让所有组的 prompt token 从第 1 轮到第 4 轮持续增长（见下表），但脚本把后续轮次的输出上限设得比开场轮短（`datasets/qira_sessions.jsonl` 中为 400 → 200 → 200 → 80 输出 token，属设计选择），两个效应在这里大体抵消。每 1,000 个会话：{cheapest['arm']} ${cheapest['session_cost_usd_per_1000_sessions']:.2f} … {dearest['arm']} ${dearest['session_cost_usd_per_1000_sessions']:.2f}。**提示缓存没有贡献：{sum(a['turns'] for a in by_arm)} 轮里 {cache_hits_total} 轮报告了缓存 token。**缓存要求存在一个早先请求已经发送过的 ≥ 1,024 token 前缀；本轮会话中之后被作为前缀重发的最长 prompt 为 {max_reusable} token。更长的对话或长系统提示词会越过这条线——本轮未测。"),
        t(f"- **Model Router in `quality` mode changed model inside {router_changes.get('router-sol-luna-quality', 0)} of {SESSIONS * ITERATIONS} conversations; in `balanced` mode {router_changes.get('router-sol-luna-balanced', 0)} of {SESSIONS * ITERATIONS}.** Quality mode's Sol share rose with the conversation: {' → '.join(f'{x:.0%}' for x in quality_sol_by_turn)} from turn 1 to turn 4, and its observed per-session sequences were {', '.join(f'`{q}`' for q in quality_seqs)}. A Qira user on quality mode would be answered by different models within one conversation — tone and formatting can shift turn to turn, and the per-turn cost is unpredictable. Balanced mode served Luna for every turn. 18 sessions per mode; treat as an observed pattern, not a rule.",
          f"- **Model Router 的 `quality` 模式在 {SESSIONS * ITERATIONS} 个会话中有 {router_changes.get('router-sol-luna-quality', 0)} 个中途换了模型；`balanced` 模式 {SESSIONS * ITERATIONS} 个中 {router_changes.get('router-sol-luna-balanced', 0)} 个。**quality 模式的 Sol 占比随对话推进上升：第 1 轮到第 4 轮为 {' → '.join(f'{x:.0%}' for x in quality_sol_by_turn)}，观察到的会话序列有 {'、'.join(f'`{q}`' for q in quality_seqs)}。使用 quality 模式的 Qira 用户在同一段对话里会被不同模型回答——语气与格式可能逐轮变化，每轮成本也不可预测。balanced 模式每一轮都由 Luna 服务。每种模式 18 个会话；这是观测到的规律，不是规则。"),
        t(f"- **Sustained load:** with continuous refill (target concurrency held for {min_share:.0%} of every counting window), the three candidates at capacity 1000 completed every request at 4/8/16 in flight; the balanced router at capacity 300 returned **{router_l[16]['errors_429']} × 429 at 16 in flight** ({router_l[8]['errors_429']} at 8, {router_l[4]['errors_429']} at 4). Capacity, not the model, sets the ceiling.",
          f"- **持续负载：**持续补充请求（目标并发在每个计数窗口内维持 {min_share:.0%}），capacity 1000 的三个候选模型在 4/8/16 并发下全部完成；capacity 300 的 balanced Router 在 16 并发下返回 **{router_l[16]['errors_429']} 次 429**（8 并发 {router_l[8]['errors_429']} 次，4 并发 {router_l[4]['errors_429']} 次）。上限由 capacity 决定，不由模型决定。"),
        t(f"- **Fallback is mandatory on PAYGO, and it must watch the stream, not just the status code.** Driving a capacity-10 Luna deployment at 8 in flight, a client with no fallback succeeded on **{none_r['success_rate']:.1%}** of requests. Every rate-limit rejection observed across the three policies arrived **after an HTTP 200, as an error event inside the stream**: {limit_in_stream} in the counted windows ({limit_all_records} including ramp and deadline-cut records), {limit_request_time} at request time, {http429} HTTP 429s. A first version of the fallback client that only retried on request-time status — the semantics of APIM's `<retry condition=\"429\">` — therefore never triggered and matched the no-fallback result (evidence kept in `outputs/resilience_v1_request_time_only/`). The corrected client switches backend on an error seen before any content is delivered: reactive succeeded on **{react_r['success_rate']:.1%}** ({react_r['in_stream_fallbacks']} in-stream switches), adding {react_r['fallback_overhead_ms_p50']:.0f} ms P50 / {react_r['fallback_overhead_ms_p90']:.0f} ms P90 to TTFT on rescued requests; proactive (route to the secondary while primary utilization ≥ 80 %, 60 s cache) succeeded on **{pro_r['success_rate']:.1%}** with {pro_r['proactive_skips']} requests never touching the primary.",
          f"- **PAYGO 上回退是必需的，而且必须盯着流本身，不能只看状态码。**用 8 并发压 capacity 10 的 Luna 部署，没有回退的客户端成功率 **{none_r['success_rate']:.1%}**。三种策略下观察到的每一次限流拒绝，**都是在 HTTP 200 之后以流内错误事件的形式到达**：计数窗口内 {limit_in_stream} 次（含预热与截止时被切断的记录共 {limit_all_records} 次），请求时 {limit_request_time} 次，HTTP 429 共 {http429} 次。第一版只在请求时按状态码重试的回退客户端——即 APIM `<retry condition=\"429\">` 的语义——因此从未触发，结果与无回退相同（证据保留在 `outputs/resilience_v1_request_time_only/`）。修正后的客户端在任何内容送达之前遇到错误即切换后端：被动回退成功率 **{react_r['success_rate']:.1%}**（{react_r['in_stream_fallbacks']} 次流内切换），被救回的请求 TTFT 增加 P50 {react_r['fallback_overhead_ms_p50']:.0f} ms / P90 {react_r['fallback_overhead_ms_p90']:.0f} ms；主动回退（主部署利用率 ≥ 80% 时直接走备用，缓存 60 s）成功率 **{pro_r['success_rate']:.1%}**，其中 {pro_r['proactive_skips']} 次请求完全没碰主部署。"),
        t(f"- **Lifecycle is a selection criterion, not a footnote:** per the Models API on {lifecycle['checked_at_utc'][:10]}, **gpt-5-mini 2025-08-07 retires {life_of['gpt-5-mini']['inference_retirement_utc'][:10]}** and **gpt-4o-mini 2024-07-18 is already deprecated for new customers** (retires {life_of['gpt-4o-mini']['inference_retirement_utc'][:10]}); the GPT-5.6 family runs to {life_of['gpt-5.6-luna']['inference_retirement_utc'][:10]}. `model-router` advertises `chatCompletion` only — consistent with the Responses-path rejection observed in #2.",
          f"- **生命周期是选型条件，不是脚注：**按 {lifecycle['checked_at_utc'][:10]} 的 Models API，**gpt-5-mini 2025-08-07 将于 {life_of['gpt-5-mini']['inference_retirement_utc'][:10]} 退役**，**gpt-4o-mini 2024-07-18 已对新客户停用**（{life_of['gpt-4o-mini']['inference_retirement_utc'][:10]} 退役）；GPT-5.6 系列到 {life_of['gpt-5.6-luna']['inference_retirement_utc'][:10]}。`model-router` 只声明 `chatCompletion` 能力——与 #2 中观察到的 Responses 路径被拒一致。"),
        "",
        t("## 1. Multi-turn sessions: what a conversation costs", "## 1. 多轮会话：一次对话的真实成本"),
        "",
        t(f"Six scripted conversations, one per Qira scenario, four dependent turns each (turn k asks about the model's own answer to turn k−1). Replayed statelessly — system message, prior user/assistant pairs, new user message — identically on Responses (three candidates) and Chat Completions (two router modes); {ITERATIONS} iterations per session; {len(SESSION_ARMS) * SESSIONS * ITERATIONS * TURNS} turns, all completed. Cost = Global list price by served model.",
          f"六个脚本化会话，每个 Qira 场景一个，各 4 轮且轮次相互依赖（第 k 轮针对模型自己在第 k−1 轮的回答提问）。无状态回放——系统消息、此前的 user/assistant 对、新的 user 消息——在 Responses（三个候选）与 Chat Completions（两个 Router 模式）上完全一致；每个会话 {ITERATIONS} 次迭代；共 {len(SESSION_ARMS) * SESSIONS * ITERATIONS * TURNS} 轮，全部完成。成本按实际服务模型的 Global 牌价。"),
        "",
        table("Arm|Prompt tokens T1 → T4|Turns with cache hit|TTFT P50 T1 → T4 s|Wait per session (ΣTTFT) P50 s|USD / 1,000 sessions|Session ÷ 4×T1|Router: Sol turns / sessions that changed model",
              "实验组|Prompt token T1 → T4|命中缓存的轮次|TTFT P50 T1 → T4 s|每会话等待（ΣTTFT）P50 s|美元 / 1,000 会话|会话 ÷ 4×T1|Router：Sol 轮占比 / 中途换模会话数", sess_tbl),
        "",
        t("- Per-turn detail (prompt/cached/output tokens, TTFT/E2E, cost, Sol share by turn): [sessions_by_turn.csv](outputs/sessions_by_turn.csv); per-session router sequences: [sessions_router_sequences.csv](outputs/sessions_router_sequences.csv).\n- \"Session ÷ 4×T1\" compares the measured 4-turn cost with four first turns. Below 1 means the follow-up turns were cheaper than the opener despite carrying the history; it is specific to these scripts, and conversations with long openers re-sent many times grow faster than linearly.\n- Zero cache hits is expected behaviour, not a defect: no request in these sessions carried a ≥ 1,024-token prefix that a previous request had already sent (`max_reusable_prefix_tokens` in [sessions_by_arm.csv](outputs/sessions_by_arm.csv)).",
          "- 逐轮明细（prompt/缓存/输出 token、TTFT/E2E、成本、各轮 Sol 占比）见 [sessions_by_turn.csv](outputs/sessions_by_turn.csv)；各会话 Router 序列见 [sessions_router_sequences.csv](outputs/sessions_router_sequences.csv)。\n- “会话 ÷ 4×T1”把实测的 4 轮成本与 4 个首轮相比。小于 1 表示后续轮次虽然带着历史，仍比开场轮便宜；这与这些脚本相关，开场很长且被多次重发的对话会增长快于线性。\n- 零缓存命中是预期行为，不是缺陷：这些会话中没有任何请求带有早先请求已发送过的 ≥ 1,024 token 前缀（见 [sessions_by_arm.csv](outputs/sessions_by_arm.csv) 的 `max_reusable_prefix_tokens`）。"),
        "",
        t("## 2. Sustained load: steady state at 4 / 8 / 16 in flight", "## 2. 持续负载：4 / 8 / 16 并发的稳态"),
        "",
        t(f"Each level runs for {load_summary['duration_s']:.0f} s with exactly N workers that start the next prompt the moment one finishes; the first {load_summary['ramp_s']:.0f} s are ramp and only requests that start after the ramp and finish before the deadline are counted (the *share of window at target* column, recomputed from per-request offsets, is how long N were actually in flight). Prompts cycle the 17 Qira scenario prompts. This replaces the closed-batch table in #3, whose ≤16 rows held the target concurrency for only 22–45 % of the wall.",
          f"每个级别运行 {load_summary['duration_s']:.0f} s，正好 N 个工作线程，一个请求结束立刻发起下一个；前 {load_summary['ramp_s']:.0f} s 为预热，只统计在预热后开始、在截止前结束的请求（“达到目标并发的窗口占比”一列由逐请求起止偏移量重算，表示 N 个请求真正同时在飞的时间比例）。提示词循环使用 17 道 Qira 场景题。本表替代 #3 中的封闭批次表——那里 ≤16 的行只有 22–45% 的时间处于目标并发。"),
        "",
        table("Arm|N|Completed|429|Window at N|req/s|Out tok/s (incl. reasoning)|Visible tok/s|TTFT P50 / P90 / P95 s|E2E P50 / P90 s",
              "实验组|N|完成|429|处于 N 并发的窗口占比|req/s|输出 tok/s（含推理）|可见 tok/s|TTFT P50 / P90 / P95 s|E2E P50 / P90 s", load_tbl),
        "",
        table("Arm|Visible tok/s 4 → 16|req/s 4 → 16|TTFT P50 4 → 16 s|TTFT P90 4 → 16 s|429 at 4 / 8 / 16",
              "实验组|可见 tok/s 4 → 16|req/s 4 → 16|TTFT P50 4 → 16 s|TTFT P90 4 → 16 s|429（4 / 8 / 16）", scale_tbl),
        "",
        t(f"- {total_429} × 429 and {total_other} other errors in total, all on the router deployment (capacity 300); the {rt['requests']} rejection payloads carry the router's own trace: `x-ratelimit-limit-requests: {', '.join(str(x) for x in rt['limit_requests'])}`, `Retry-After` {min(rt['retry_after_s']):.0f}–{max(rt['retry_after_s']):.0f} s, and **exactly one attempt each** ({', '.join(f'{k}: {v}' for k, v in rt['models_tried'].items())}; attempts-per-request histogram {rt['attempts_histogram']}) — the router did not retry on Sol when its Luna attempt was throttled. The capacity-1000 candidates did not reach their limits at 16 in flight with these prompt lengths. A production capacity plan has to start from the deployment's RPM/TPM setting and the observed req/s per concurrent user, not from the model.\n- The router's aggregate includes the reasoning tokens its Luna runs with by default (no effort sent); compare it on the *visible* column against `gpt-5.6-luna@none`.\n- Single deployment, single region, one client: this is the deployment's steady state on this day, not an SLA. Per-request records with offsets (answer text replaced by its SHA256, see §6): [{raw_path('sustained', run_l).relative_to(ROOT).as_posix()}]({raw_path('sustained', run_l).relative_to(ROOT).as_posix()}).",
          f"- 总计 {total_429} 次 429、{total_other} 次其他错误，全部发生在 Router 部署上（capacity 300）；这 {rt['requests']} 条拒绝响应体里带有 Router 自己的 trace：`x-ratelimit-limit-requests: {', '.join(str(x) for x in rt['limit_requests'])}`、`Retry-After` {min(rt['retry_after_s']):.0f}–{max(rt['retry_after_s']):.0f} s，并且**每条只有一次尝试**（{', '.join(f'{k}: {v}' for k, v in rt['models_tried'].items())}；每请求尝试次数分布 {rt['attempts_histogram']}）——Luna 尝试被限流时，Router 没有改试 Sol。capacity 1000 的候选模型在 16 并发、当前提示词长度下没有触及上限。生产容量规划要从部署的 RPM/TPM 设置和每个并发用户的实测 req/s 出发，而不是从模型出发。\n- Router 的聚合值包含其 Luna 默认运行时的推理 token（未发送 effort）；与 `gpt-5.6-luna@none` 比较请看“可见 tok/s”列。\n- 单部署、单区域、单客户端：这是这些部署当天的稳态，不是 SLA。带起止偏移量的逐请求记录（回答正文以其 SHA256 代替，见第 6 节）：[{raw_path('sustained', run_l).relative_to(ROOT).as_posix()}]({raw_path('sustained', run_l).relative_to(ROOT).as_posix()})。"),
        "",
        t("## 3. Rate limit and fallback: three client policies against a throttled primary", "## 3. 限流与回退：三种客户端策略对被限流主部署的表现"),
        "",
        t(f"`{chain[0]}` is the same gpt-5.6-luna model deployed at DataZoneStandard capacity 10 (service rate limits: 10 requests/min, 10,000 tokens/min), so the PAYGO limit is reachable at trivial cost. `{chain[1]}` (GlobalStandard, capacity 1000) is the secondary. {res_summary['level']} workers for {res_summary['duration_s']:.0f} s per policy ({res_summary['ramp_s']:.0f} s ramp excluded), {res_summary['failure_pause_s']:.0f} s pause after a failed request, {res_summary['settle_seconds'] if 'settle_seconds' in res_summary else 70} s between policies so the primary's minute window resets. The client is [fallback_client.py](fallback_client.py). It takes the two ideas of the reference APIM policy in [config/apim-policy-ptu-routing.reference.xml](config/apim-policy-ptu-routing.reference.xml) — retry on a second backend when the first fails, and route ahead of the limit from the `x-ratelimit-*` headers — and **extends both**: it switches on an in-stream error before the first content delta (the XML's `<retry condition=\"429\">` only sees the HTTP status, which was 200 here), and its utilization is the worse of *request* and *token* utilization (the XML reads tokens only; on this RPM-bound deployment token utilization stayed near 10 %, so that policy as written would not have gone proactive). Hosting the same logic in API Management therefore needs a policy revised on both points; it was not deployed or tested here.",
          f"`{chain[0]}` 是同一个 gpt-5.6-luna 模型以 DataZoneStandard capacity 10 部署（服务端限流：10 请求/分、10,000 token/分），因此 PAYGO 限流用极低成本就能触及。`{chain[1]}`（GlobalStandard、capacity 1000）为备用。每种策略 {res_summary['level']} 个工作线程运行 {res_summary['duration_s']:.0f} s（不计 {res_summary['ramp_s']:.0f} s 预热），请求失败后停 {res_summary['failure_pause_s']:.0f} s，策略之间停 {res_summary['settle_seconds'] if 'settle_seconds' in res_summary else 70} s 让主部署的分钟窗口复位。客户端为 [fallback_client.py](fallback_client.py)。它取了 [config/apim-policy-ptu-routing.reference.xml](config/apim-policy-ptu-routing.reference.xml) 这份参考 APIM 策略的两个思路——第一个后端失败时改走第二个后端、根据 `x-ratelimit-*` 响应头在触限之前提前分流——并**在两点上做了扩展**：在第一个内容增量之前遇到流内错误即切换（XML 的 `<retry condition=\"429\">` 只看 HTTP 状态，本轮状态是 200），以及利用率取*请求*与*token* 两者中较高者（XML 只读 token；在这个受 RPM 约束的部署上 token 利用率始终约 10%，按原文策略不会触发主动分流）。若要把同样逻辑放进 API Management，策略需在这两点上修订；本轮未部署、未测试 APIM。"),
        "",
        table("Policy|Completed|Success|Rate-limit failed|Served by primary|Served by fallback|In-stream switches|Proactive skips|TTFT P50 primary s|TTFT P50 fallback s|Fallback overhead P50 / P90 ms|req/s",
              "策略|完成|成功率|限流失败|主部署服务|备用服务|流内切换|主动跳过|TTFT P50 主 s|TTFT P50 备 s|回退开销 P50 / P90 ms|req/s", res_tbl),
        "",
        t(f"- **The shape of the rejection matters more than the number.** On the streaming Responses path the DataZone deployment accepted every request (HTTP 200) and then emitted `Your requests to gpt-5.6-luna for {chain[0]} in swedencentral have exceeded rate limit.` as an in-stream error; because the HTTP exchange had already succeeded there is no `Retry-After` header for the client to read. The Chat Completions router deployment in section 2 rejected with a real HTTP 429 (`Retry-After: 3`). A gateway or client that only inspects the response status will pass the in-stream case through to the user as a failed answer; the retry has to happen at the first stream event. This is the single most important finding of the run for anyone wiring Qira to PAYGO.\n- **What the customer should take from this:** a second backend plus a client (or gateway) that switches on *any* failure before the first content delta. Without it, {1 - none_r['success_rate']:.1%} of requests in this run failed outright. With it, the {react_r['fallback_share_of_ok']:.0%} of requests that fell back saw TTFT {s(react_r['ttft_fallback_p50_ms'])} s P50 = the primary's rejection latency ({react_r['fallback_overhead_ms_p50']:.0f} ms P50) + the secondary's own first token ({s(react_r['ttft_fallback_net_of_overhead_p50_ms'])} s P50 net of that overhead; the secondary undisturbed measured {s(pro_r['ttft_fallback_p50_ms'])} s under proactive routing and {s(router_undisturbed_ttft)} s in section 2 at 8 in flight).\n- **Proactive routing removes the rejection latency** for requests routed straight to the secondary, at the price of reading rate-limit headers on every primary response and re-probing the primary when the 60 s cache expires (the reference policy's `duration=\"60\"`). Whether that is worth {react_r['fallback_overhead_ms_p50']:.0f} ms depends on the product's TTFT budget.\n- **Model Router does not fall back on a deployment-level 429.** The {rt['requests']} router rejections in section 2 each carry a trace with a single attempt (`gpt-5.6-luna → 429`) and no retry on Sol; across 1,128 routed requests in #2 and the router runs here, the trace never showed a second attempt. Its behaviour when an underlying model itself is down remains **not observed**. Either way, the client-side fallback above is what handles it.\n- Same model on both sides of the chain, so quality is unchanged by fallback; a cross-model fallback (e.g. to gpt-4o-mini) uses the same mechanism but changes the answer — score it before adopting it.",
          f"- **拒绝的形态比次数更重要。**在流式 Responses 路径上，DataZone 部署接受了每个请求（HTTP 200），然后在流内发出 `Your requests to gpt-5.6-luna for {chain[0]} in swedencentral have exceeded rate limit.` 错误事件；HTTP 交换已经成功，客户端读不到 `Retry-After` 响应头。第 2 节中 Chat Completions 的 Router 部署则返回真正的 HTTP 429（`Retry-After: 3`）。只检查响应状态码的网关或客户端会把流内这种情况当作失败回答直接透给用户；重试必须发生在第一个流事件处。对任何要把 Qira 接到 PAYGO 上的人来说，这是本轮最重要的一条发现。\n- **客户应当带走的结论：**需要第二个后端，加上一个在第一个内容增量之前遇到*任何*失败就切换的客户端（或网关）。没有它，本轮 {1 - none_r['success_rate']:.1%} 的请求直接失败。有了它，回退的 {react_r['fallback_share_of_ok']:.0%} 请求 TTFT P50 为 {s(react_r['ttft_fallback_p50_ms'])} s = 主部署拒绝耗时（P50 {react_r['fallback_overhead_ms_p50']:.0f} ms）+ 备用部署自身的首字（扣除该开销后 P50 {s(react_r['ttft_fallback_net_of_overhead_p50_ms'])} s；备用部署不受干扰时，主动路由下测得 {s(pro_r['ttft_fallback_p50_ms'])} s，第 2 节 8 并发下为 {s(router_undisturbed_ttft)} s）。\n- **主动路由省掉拒绝耗时**（直接走备用的请求），代价是每次主部署响应都要读限流响应头，并在 60 s 缓存过期后重新探测主部署（对应参考策略的 `duration=\"60\"`）。是否值 {react_r['fallback_overhead_ms_p50']:.0f} ms，取决于产品的 TTFT 预算。\n- **Model Router 不会在部署级 429 上回退。**第 2 节的 {rt['requests']} 次 Router 拒绝，每条 trace 只有一次尝试（`gpt-5.6-luna → 429`），没有改试 Sol；#2 的 1,128 次路由请求加本轮的 Router 运行中，trace 从未出现第二次尝试。底层模型本身故障时的表现**未被观测**。无论哪种情况，都由上面的客户端回退来处理。\n- 链两端是同一个模型，回退不改变质量；跨模型回退（例如到 gpt-4o-mini）机制相同但答案会变，采用前须先评分。"),
        "",
        t("## 4. Lifecycle and API compatibility (Models API, not prose)", "## 4. 生命周期与 API 兼容性（来自 Models API，非文档转述）"),
        "",
        t(f"Queried with [scripts/query_model_lifecycle.py](scripts/query_model_lifecycle.py) on {lifecycle['checked_at_utc'][:10]}; raw answer in [model_lifecycle_swedencentral.json](outputs/model_lifecycle_swedencentral.json). `Deprecated` here means the documented state \"existing customers only\" (API value `Deprecating`); dates are the service's programmatic values on that day and can move. Standard lifecycle is 18 months GA → deprecated at 12 → retired.",
          f"于 {lifecycle['checked_at_utc'][:10]} 用 [scripts/query_model_lifecycle.py](scripts/query_model_lifecycle.py) 查询；原始结果见 [model_lifecycle_swedencentral.json](outputs/model_lifecycle_swedencentral.json)。此处“已停用”对应文档定义的“仅限现有客户”状态（API 值 `Deprecating`）；日期是当天服务端的程序化取值，可能变动。标准生命周期为 GA 后 18 个月，第 12 个月起对新客户停用。"),
        "",
        table("Model|Version|Lifecycle|Inference retirement|API capabilities|Deployment SKUs|Role in these studies",
              "模型|版本|生命周期|推理退役日期|API 能力|部署 SKU|在本系列中的角色", life_tbl),
        "",
        t("- For a workload going live in Q4 2026, gpt-5-mini 2025-08-07 leaves under six months of runway and gpt-4o-mini 2024-07-18 can no longer be deployed by a subscription that has not used it before. Both were the cheap/fast candidates in #1; their price-performance has to be weighed against a forced migration inside the first year.\n- All three candidate models and Sol expose `chatCompletion`, `responses` and `assistants`; `model-router` exposes `chatCompletion` only. In the Sweden Central listing gpt-5.6-luna is not offered on regional `ProvisionedManaged` (only Global/DataZone Standard and Global Provisioned) — relevant if the PTU discussion resumes; SKU availability is region-scoped and should be re-queried for the target region.",
          "- 对于 2026 年第四季度上线的负载，gpt-5-mini 2025-08-07 剩余不到六个月，gpt-4o-mini 2024-07-18 对从未部署过它的订阅已不可再部署。二者正是 #1 中便宜/快速的候选；其性价比要和上线首年内被迫迁移放在一起权衡。\n- 三个候选模型和 Sol 都提供 `chatCompletion`、`responses`、`assistants`；`model-router` 只提供 `chatCompletion`。在 Sweden Central 的目录中，gpt-5.6-luna 不提供区域级 `ProvisionedManaged`（只有 Global/DataZone Standard 与 Global Provisioned）——若 PTU 讨论重启，这点相关；SKU 可用性按区域列出，目标区域应重新查询。"),
        "",
        t("## 5. Reproduce", "## 5. 复现"),
        "",
        "```bash",
        "# offline: rebuild every CSV and both READMEs from the retained evidence; run the tests",
        "python scripts/build_readiness_report.py",
        "python -m unittest discover -s tests",
        "",
        "# live (paid): same-region Linux VM, Entra identity with Cognitive Services OpenAI User on the resource",
        "export AZURE_OPENAI_ENDPOINT=\"https://<RESOURCE_NAME>.openai.azure.com/\"",
        "python sessions.py --dataset datasets/qira_sessions.jsonl \\",
        "  --arms \"gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat,router-sol-luna-quality#chat\" \\",
        "  --iterations 3 --region swedencentral --client-location swedencentral-linux-vm",
        "python sustained_load.py --dataset datasets/qira_scenarios.jsonl \\",
        "  --arms \"gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat\" \\",
        "  --levels 4,8,16 --duration 90 --ramp 15 --region swedencentral --client-location swedencentral-linux-vm",
        "# create a capacity-10 deployment of the primary model first (az cognitiveservices account deployment create ... --sku-capacity 10)",
        "python resilience_test.py --dataset datasets/qira_scenarios.jsonl --primary gpt-5.6-luna-lowcap --fallback gpt-5.6-luna \\",
        "  --effort none --policies none,reactive,proactive --level 8 --duration 75 --ramp 15 --region swedencentral --client-location swedencentral-linux-vm",
        "python scripts/query_model_lifecycle.py --region swedencentral",
        "```",
        "",
        t("## 6. Evidence and boundaries", "## 6. 证据与边界"),
        "",
        t(f"- Full answers with `response_sha256`: `outputs/raw_fulltext/sessions_{run_s}.jsonl` and `resilience_{run_r}.jsonl`. The sustained run is retained **numerically** (`sustained_{run_l}.numeric.jsonl`: every field except `response_text`, whose SHA256 is kept per record); its 3,770 answers to the same 17 prompts remain on the deallocated VM disk (`sustained_fulltext_retained_on_vm_only.sha256` in the provenance) and were not moved through the management-plane transfer channel. Live summaries written by the runners are cross-checked by the builder and kept beside the records. First fallback attempt (request-time-only client) kept under `outputs/resilience_v1_request_time_only/`. Hashes in [provenance_readiness.json](outputs/provenance_readiness.json); resource state after the run in [resource_closeout_readiness.json](outputs/resource_closeout_readiness.json).\n- Synthetic English prompts and scripted conversations, not customer traffic; one client VM; TTFT/E2E are client-observed and include network and delivery; costs are Global list-price estimates by served model (DataZone premium and router fee excluded), not invoices.\n- Not measured: conversations long enough to trigger prompt caching; concurrency beyond 16 or durations beyond 90 s; the 429 onset of the capacity-1000 deployments; whether the in-stream rate-limit shape also applies to GlobalStandard deployments or to Chat Completions on a direct model (the router deployment on Chat returned HTTP 429); Model Router behaviour when an underlying model is unavailable; cross-model fallback quality; API Management itself (the reference policy was neither deployed nor executed — the in-process client reproduces its intent with the two extensions described in §3).",
          f"- 带 `response_sha256` 的全文回答：`outputs/raw_fulltext/sessions_{run_s}.jsonl` 与 `resilience_{run_r}.jsonl`。持续负载运行以**数值形式**保留（`sustained_{run_l}.numeric.jsonl`：除 `response_text` 外的全部字段，每条记录保留其 SHA256）；对同样 17 道题的 3,770 条回答仍留在已释放的 VM 磁盘上（其哈希见来源文件的 `sustained_fulltext_retained_on_vm_only.sha256`），未经管理平面传输通道搬运。运行器实时写出的汇总由生成器交叉校验并一并保留。第一次回退尝试（只看请求时状态的客户端）保留在 `outputs/resilience_v1_request_time_only/`。哈希见 [provenance_readiness.json](outputs/provenance_readiness.json)；运行后的资源状态见 [resource_closeout_readiness.json](outputs/resource_closeout_readiness.json)。\n- 合成英文提示词与脚本化会话，不是客户流量；单台客户端 VM；TTFT/E2E 为客户端观测值，包含网络与交付；成本为按实际服务模型的 Global 牌价估算（不含 DataZone 溢价与路由费），不是账单。\n- 未测：长到足以触发提示缓存的会话；超过 16 的并发或超过 90 s 的时长；capacity 1000 部署的 429 出现点；流内限流形态是否同样出现在 GlobalStandard 部署或直连模型的 Chat Completions 上（Chat 上的 Router 部署返回的是 HTTP 429）；底层模型不可用时 Model Router 的表现；跨模型回退的质量；API Management 本身（参考策略既未部署也未执行——进程内客户端按第 3 节所述的两点扩展重现了它的意图）。"),
    ]
    (ROOT / "README.md").write_text(render("en", blocks), encoding="utf-8", newline="\n")
    (ROOT / "README-CN.md").write_text(render("zh", blocks), encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if the READMEs would change")
    args = parser.parse_args()
    before = {n: (ROOT / n).read_text(encoding="utf-8") if (ROOT / n).exists() else None for n in ("README.md", "README-CN.md")}
    run_s, s_rows, by_turn, by_arm, sequences = sessions_report()
    run_l, l_rows, l_summary, load_rows = sustained_report()
    run_r, r_rows, r_summary, res_rows = resilience_report()
    lifecycle = lifecycle_table()
    provenance = {
        "runs": {"sessions": run_s, "sustained": run_l, "resilience": run_r},
        "lifecycle_checked_at_utc": lifecycle["checked_at_utc"],
        "sustained_fulltext_retained_on_vm_only": {
            "file": f"outputs/sustained_{run_l}.jsonl (VM: /home/azureuser/bench)",
            "sha256": "888404fd02409055341cd004e35f0148720ce60d0968ac6313f3dbb00526a7f5",
            "rows": 3770,
            "reason": "896 KB compressed; management-plane transfer channel moves ~50 KB per 5 min and the subscription policy forces publicNetworkAccess=Disabled on storage accounts. Numeric records with per-answer SHA256 were transferred instead.",
        },
        "vm_transfer_sha256_before_redaction": {
            f"sessions_{run_s}.jsonl.xz": "e9019b74ec36269ac01463319d2b31c80a3a872003091a764d9815c7ea874b68",
            f"sustained_{run_l}.numeric.jsonl.xz": "ead7b3da4ec2363626aa551d78faf705d26e09756ba744e390d1c7a8e011b741",
            f"sustained_{run_l}.summary.json": "4f41465903ec03a215180a726d1dcdd5d115887b17ebeb076fcbbd7f5ae21aef",
            "resilience_v1.tar.xz": "dddf1ceffda2c35fc333487e4ffd230b319c56b662c00045575c2416e6d402f5",
        },
        "redaction": "endpoint_host replaced by YOUR-ENDPOINT.cognitiveservices.azure.com after transfer; response_sha256 hashes response_text only.",
        "sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): sha(p) for p in sorted(OUTPUT.rglob("*")) if p.is_file() and p.name != "provenance_readiness.json"},
    }
    (OUTPUT / "provenance_readiness.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8", newline="\n")
    build_readmes(run_s, by_turn, by_arm, sequences, run_l, load_rows, l_summary, run_r, res_rows, r_summary, lifecycle)
    after = {n: (ROOT / n).read_text(encoding="utf-8") for n in before}
    if args.check and any(before[n] != after[n] for n in before):
        raise SystemExit("README drift: regenerate and commit.")
    print(f"VERIFIED: sessions {len(s_rows)} turns, sustained {len(l_rows)} records ({sum(r['counted'] for r in load_rows)} counted), "
          f"resilience {len(r_rows)} records ({sum(r['counted'] for r in res_rows)} counted), lifecycle {len(lifecycle['models'])} models; "
          f"{len(by_turn) + len(by_arm) + len(load_rows) + len(res_rows)} summary rows written.")


if __name__ == "__main__":
    main()
