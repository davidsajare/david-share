"""Validate the follow-up runs and generate the comparison CSVs plus both READMEs.

Follow-up to the scenario benchmark and the router validation in the sibling folders.
Three paid runs were made on the same Sweden Central VM, with the hardened harness/judge:

  1. direct_<RUN_DIRECT>.jsonl   gpt-5.6-sol-dz / gpt-5.6-luna-dz re-measured on the
                                 Responses API (the router study measured them on Chat
                                 Completions and saw whole-answer bursts).
  2. loadtest_<RUN_LOAD>.jsonl   stepped concurrency 1/4/8/16 for the three Task A
                                 candidates and the balanced router (loadtest.py).
  3. quality_v2_*.jsonl          every Task A and Task B answer re-judged with rubric
                                 v2 (explicit tool-less action rule).

Every check raises: a table is not produced from a run that cannot prove its matrix.

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import argparse
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
import judge  # noqa: E402

OUTPUT = ROOT / "outputs"
INPUTS = OUTPUT / "inputs"
RAW = OUTPUT / "raw_fulltext"
RUN_B, RUN_A = "20260909_223737", "20260909_120534"
DIRECT_ARMS = ("gpt-5.6-sol-dz", "gpt-5.6-sol-dz@low", "gpt-5.6-luna-dz", "gpt-5.6-luna-dz@low")
LOAD_ARMS = ("gpt-4o-mini-bench", "gpt-5-mini@minimal", "gpt-5.6-luna@none", "router-sol-luna-balanced")
LEVELS = (1, 4, 8, 16)
REQUESTS_PER_LEVEL = 48
ACTION_QUESTIONS = {"S03", "S04"}  # tool-less action requests that exposed the v1 rubric drift


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


def validate_input_manifest():
    manifest = json.loads((INPUTS / "manifest.json").read_text(encoding="utf-8"))
    for name, entry in manifest["files"].items():
        path = INPUTS / name
        if not path.is_file():
            raise ValueError(f"Historical input is missing: {name}")
        actual = sha(path)
        if actual != entry["sha256"]:
            raise ValueError(
                f"Historical input {name} has SHA256 {actual}; expected {entry['sha256']}.")


def find_run(prefix: str) -> str:
    runs = sorted(p.name[len(prefix) + 1:-6] for p in RAW.glob(f"{prefix}_*.jsonl"))
    if len(runs) != 1:
        raise ValueError(f"Expected exactly one {prefix} run under outputs/raw_fulltext, found {runs}")
    return runs[0]


def require_single_environment(rows, api):
    conditions = {(r["region"], r["endpoint_host"], r["deployment_type"], r["client_location"], r.get("api")) for r in rows}
    if len(conditions) != 1 or next(iter(conditions))[0] != "swedencentral" or next(iter(conditions))[4] != api:
        raise ValueError(f"Mixed or unexpected environment: {conditions}")
    if any(r["tools_enabled"] is not False for r in rows):
        raise ValueError("A record had tools enabled.")


def valid(r):
    return not r.get("error") and r.get("status") == "completed" and not r.get("truncated") and r["prompt_tokens"] > 0


def latency_row(rows):
    ttft = [r["ttft_ms"] for r in rows if r["ttft_ms"] is not None]
    tpot = [r["tpot_ms"] for r in rows if r.get("tpot_ms") is not None]
    tps = [r["decode_tps"] for r in rows if r.get("decode_tps") is not None]
    dec = [r["decode_ms"] for r in rows if r.get("decode_ms") is not None]
    return {
        "n": len(rows),
        "ttft_p50_ms": pct(ttft, 50), "ttft_p90_ms": pct(ttft, 90), "ttft_p95_ms": pct(ttft, 95),
        "e2e_p50_ms": pct([r["e2e_ms"] for r in rows], 50), "e2e_p90_ms": pct([r["e2e_ms"] for r in rows], 90),
        "e2e_p95_ms": pct([r["e2e_ms"] for r in rows], 95),
        "decode_span_p50_ms": pct(dec, 50), "bursts_lt50ms": sum(1 for d in dec if d < 50),
        "tpot_p50_ms": pct(tpot, 50), "tpot_p90_ms": pct(tpot, 90), "tok_per_s_p50": pct(tps, 50),
        "output_tokens_mean": round(statistics.mean(r["completion_tokens"] for r in rows), 1),
        "reasoning_tokens_mean": round(statistics.mean(r["reasoning_tokens"] for r in rows), 1),
    }


# ---------------------------------------------------------------------------- 1. direct API path
def direct_comparison(pricing, registry):
    run = find_run("direct")
    new = load_jsonl(RAW / f"direct_{run}.jsonl")
    if len(new) != 752 or Counter(r["arm"] for r in new) != Counter({a: 188 for a in DIRECT_ARMS}):
        raise ValueError(f"Responses re-measure is not the full 4 x 47 x 4 matrix ({len(new)} rows).")
    require_single_environment(new, "responses")
    keys = {(r["arm"], r["question_id"], r["iteration"]) for r in new}
    if len(keys) != 752:
        raise ValueError("Duplicate cells in the re-measure.")
    bad = [r for r in new if not valid(r)]
    if bad:
        raise ValueError(f"{len(bad)} invalid records in the re-measure: {[(r['arm'], r['question_id'], r['error']) for r in bad[:3]]}")
    with (INPUTS / f"chat_direct_requests_{RUN_B}.csv").open(encoding="utf-8") as stream:
        old = list(csv.DictReader(stream))
    for r in old:
        for k in ("ttft_ms", "e2e_ms", "decode_ms", "tpot_ms", "decode_tps"):
            r[k] = float(r[k]) if r[k] not in ("", "None") else None
        for k in ("prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens", "iteration"):
            r[k] = int(r[k])
        r["warmup"] = r["warmup"] == "True"
        r["truncated"] = r["truncated"] == "True"
    if len(old) != 752:
        raise ValueError("Historical Chat-path input is incomplete.")
    analyze._REGISTRY = registry
    rows = []
    for arm in DIRECT_ARMS:
        for label, source in (("chat_completions_2026-09-09", old), ("responses_" + run[:8], new)):
            group = [r for r in source if r["arm"] == arm and not r["warmup"]]
            if len(group) != 141:
                raise ValueError(f"{arm} {label}: expected 141 measured rows, found {len(group)}")
            model = registry[arm.split("@")[0]]["model_name"]
            cost = statistics.mean(analyze.cost_for(pricing, model, r["prompt_tokens"], r["cached_tokens"], r["completion_tokens"]) for r in group)
            rows.append({"arm": arm, "api_path": label, **latency_row(group), "cost_usd_per_1000_requests": round(cost * 1000, 4)})
    write_csv(OUTPUT / "direct_api_path_comparison.csv", rows)
    return run, new, rows


# ---------------------------------------------------------------------------- 2. load test
def load_test():
    run = find_run("loadtest")
    records = load_jsonl(RAW / f"loadtest_{run}.jsonl")
    summary = json.loads((OUTPUT / f"loadtest_{run}.summary.json").read_text(encoding="utf-8"))
    expected = len(LOAD_ARMS) * (1 + len(LEVELS) * REQUESTS_PER_LEVEL)
    if len(records) != expected:
        raise ValueError(f"Load test has {len(records)} records, expected {expected}.")
    # API surface differs per arm by design (router requires Chat), so only region/endpoint/tools are common.
    if {r["region"] for r in records} != {"swedencentral"} or len({r["endpoint_host"] for r in records}) != 1:
        raise ValueError("Load test spans more than one environment.")
    if any(r["tools_enabled"] is not False for r in records):
        raise ValueError("Load test record had tools enabled.")
    rows = []
    for arm in LOAD_ARMS:
        for level in LEVELS:
            group = [r for r in records if r["arm"] == arm and r["level"] == level and not r["warmup"]]
            if len(group) != REQUESTS_PER_LEVEL:
                raise ValueError(f"{arm} level {level}: {len(group)} records")
            ok = [r for r in group if valid(r)]
            errors = [r for r in group if r["error"]]
            wall = (max(r["finished_offset_ms"] for r in group) - min(r["started_offset_ms"] for r in group)) / 1000
            s = next(x for x in summary["summaries"] if x["arm"] == arm and x["level"] == level)
            if s["ok"] != len(ok) or s["errors"] != len(errors):
                raise ValueError(f"summary.json disagrees with records for {arm} level {level}")
            if abs(s["wall_s"] - wall) > 0.5:
                raise ValueError(f"wall-clock mismatch for {arm} level {level}: {s['wall_s']} vs {wall:.2f}")
            out_tokens = sum(r["completion_tokens"] for r in ok)
            # Closed batch: in-flight equals the target level only until the queue drains.
            # Sweep start/finish events to measure how much of the wall the target level actually held.
            events = sorted([(r["started_offset_ms"], 1) for r in group] + [(r["finished_offset_ms"], -1) for r in group])
            in_flight, prev_t, at_target_ms, peak = 0, events[0][0], 0.0, 0
            for ts, delta in events:
                if in_flight >= level:
                    at_target_ms += ts - prev_t
                in_flight += delta
                peak = max(peak, in_flight)
                prev_t = ts
            if peak != level:
                raise ValueError(f"{arm} level {level}: peak in-flight {peak} != {level}")
            rows.append({
                "arm": arm, "api": group[0]["api"], "level": level, "requests": len(group), "ok": len(ok),
                "errors": len(errors), "errors_429": sum(1 for r in errors if r["error_class"] == "429"),
                "other_errors": sum(1 for r in errors if r["error_class"] != "429"),
                "truncated": sum(1 for r in group if r["truncated"]),
                "wall_s": round(wall, 2), "peak_in_flight": peak,
                "share_of_wall_at_target": round(at_target_ms / 1000 / wall, 3),
                "requests_per_s": round(len(ok) / wall, 3),
                "aggregate_output_tok_per_s": round(out_tokens / wall, 1),
                "aggregate_visible_tok_per_s": round(sum(r["completion_tokens"] - r["reasoning_tokens"] for r in ok) / wall, 1),
                "reasoning_share_of_output": round(sum(r["reasoning_tokens"] for r in ok) / out_tokens, 3) if out_tokens else None,
                **{k: v for k, v in latency_row(ok).items() if k != "n"},
                "served_families": "|".join(sorted({r["served_model_family"] for r in ok if r["served_model_family"]})),
                "cost_usd_sum": round(sum(r["cost_usd"] or 0 for r in ok), 6),
            })
    write_csv(OUTPUT / "loadtest_levels.csv", rows)
    return run, records, rows


# ---------------------------------------------------------------------------- 3. judge v1 vs v2
def rubric_comparison():
    out_arms, out_questions = [], []
    per_task = {}
    for task, run, expected in (("B", RUN_B, 470), ("A", RUN_A, 187)):
        v1 = {(q["arm"], q["question_id"]): q for q in load_jsonl(INPUTS / f"quality_v1_{run}.jsonl")}
        v2_rows = load_jsonl(OUTPUT / f"quality_v2_{run}.jsonl")
        v2 = {(q["arm"], q["question_id"]): q for q in v2_rows}
        if len(v1) != expected or len(v2) != expected or set(v1) != set(v2):
            raise ValueError(f"Task {task}: v1/v2 coverage differs ({len(v1)} vs {len(v2)}).")
        if any(q.get("error") for q in v2_rows) or any(q.get("rubric_version") != judge.RUBRIC_VERSION for q in v2_rows):
            raise ValueError(f"Task {task}: v2 rows with errors or wrong rubric version.")
        for k in v1:
            if v1[k]["iteration"] != v2[k]["iteration"]:
                raise ValueError(f"Task {task}: v1 and v2 judged different iterations for {k}.")
            for d in analyze.QUALITY_DIMS:
                if type(v2[k][d]) is not int or not 1 <= v2[k][d] <= 5:
                    raise ValueError(f"Task {task}: invalid v2 score {k} {d}")
        arms = sorted({a for a, _ in v1})
        for arm in arms:
            qs = sorted(q for a, q in v1 if a == arm)
            m1 = statistics.mean(v1[(arm, q)]["quality_mean"] for q in qs)
            m2 = statistics.mean(v2[(arm, q)]["quality_mean"] for q in qs)
            out_arms.append({"task": task, "arm": arm, "n": len(qs), "v1_mean": round(m1, 4), "v2_mean": round(m2, 4),
                             "delta": round(m2 - m1, 4),
                             "v1_share_5": round(sum(v1[(arm, q)]["quality_mean"] == 5 for q in qs) / len(qs), 3),
                             "v2_share_5": round(sum(v2[(arm, q)]["quality_mean"] == 5 for q in qs) / len(qs), 3),
                             "v1_min": min(v1[(arm, q)]["quality_mean"] for q in qs), "v2_min": min(v2[(arm, q)]["quality_mean"] for q in qs)})
        for (arm, q), row in sorted(v1.items()):
            out_questions.append({"task": task, "arm": arm, "question_id": q, "iteration": row["iteration"],
                                  "v1_quality_mean": row["quality_mean"], "v2_quality_mean": v2[(arm, q)]["quality_mean"],
                                  "delta": round(v2[(arm, q)]["quality_mean"] - row["quality_mean"], 3),
                                  "v1_instruction_following": row["instruction_following"],
                                  "v2_instruction_following": v2[(arm, q)]["instruction_following"],
                                  "action_request": q in ACTION_QUESTIONS})
        ranking1 = [a for a, _ in sorted(((a, statistics.mean(v1[(a, q)]["quality_mean"] for _, q in v1 if _ == a)) for a in arms), key=lambda t: -t[1])]
        ranking2 = [a for a, _ in sorted(((a, statistics.mean(v2[(a, q)]["quality_mean"] for _, q in v2 if _ == a)) for a in arms), key=lambda t: -t[1])]
        action = {q: [(v1[k]["instruction_following"], v2[k]["instruction_following"]) for k in v1 if k[1] == q] for q in ACTION_QUESTIONS if any(k[1] == q for k in v1)}
        # The README attributes S04's sub-5 scores to "gave the path without stating the limitation";
        # require the judge's own justification to say so, otherwise the sentence must be rewritten.
        for k in v1:
            if k[1] == "S04" and v2[k]["instruction_following"] < 5 and "state" not in v2[k].get("justification", "").lower():
                raise ValueError(f"S04 low score for {k[0]} is not explained by a 'does not state' justification: {v2[k].get('justification')!r}")
        per_task[task] = {"ranking_v1": ranking1, "ranking_v2": ranking2,
                          "action_spreads": {q: {"v1": (min(a for a, _ in v), max(a for a, _ in v)), "v2": (min(b for _, b in v), max(b for _, b in v)),
                                                 "v2_scores": sorted(b for _, b in v)} for q, v in action.items()},
                          "mean_abs_delta": round(statistics.mean(abs(v2[k]["quality_mean"] - v1[k]["quality_mean"]) for k in v1), 4),
                          "cells_changed": sum(v2[k]["quality_mean"] != v1[k]["quality_mean"] for k in v1), "cells": len(v1)}
    write_csv(OUTPUT / "judge_rubric_comparison_arms.csv", out_arms)
    write_csv(OUTPUT / "judge_rubric_comparison_questions.csv", out_questions)
    return out_arms, out_questions, per_task


# ---------------------------------------------------------------------------- README
def t(en, zh):
    return {"en": en, "zh": zh}


def render(lang, blocks):
    out = []
    for b in blocks:
        out.append(b[lang] if isinstance(b, dict) else b)
    return "\n".join(out).rstrip("\n") + "\n"


def table(header_en, header_zh, rows):
    def build(h):
        cols = h.split("|")
        lines = ["| " + " | ".join(cols) + " |", "|" + "|".join("---:" if i else "---" for i in range(len(cols))) + "|"]
        lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
        return "\n".join(lines)
    return {"en": build(header_en), "zh": build(header_zh)}


def ms(v):
    return f"{v / 1000:.2f}"


def build_readmes(run_direct, direct_rows, run_load, load_rows, arm_rows, per_task, provenance):
    by = {(r["arm"], r["api_path"][:4]): r for r in direct_rows}
    direct_tbl = []
    for arm in DIRECT_ARMS:
        c, r = by[(arm, "chat")], by[(arm, "resp")]
        direct_tbl.append([arm, "Chat", f"{ms(c['ttft_p50_ms'])} / {ms(c['ttft_p90_ms'])}", f"{ms(c['e2e_p50_ms'])} / {ms(c['e2e_p90_ms'])}",
                           f"{c['tpot_p50_ms']:.2f}", f"{c['tok_per_s_p50']:.0f}", f"{c['bursts_lt50ms']}/141", f"{c['output_tokens_mean']:.0f}", f"{c['cost_usd_per_1000_requests']:.2f}"])
        direct_tbl.append([arm, "Responses", f"{ms(r['ttft_p50_ms'])} / {ms(r['ttft_p90_ms'])}", f"{ms(r['e2e_p50_ms'])} / {ms(r['e2e_p90_ms'])}",
                           f"{r['tpot_p50_ms']:.2f}", f"{r['tok_per_s_p50']:.0f}", f"{r['bursts_lt50ms']}/141", f"{r['output_tokens_mean']:.0f}", f"{r['cost_usd_per_1000_requests']:.2f}"])
    resp_bursts = sum(by[(a, "resp")]["bursts_lt50ms"] for a in DIRECT_ARMS)
    chat_bursts = sum(by[(a, "chat")]["bursts_lt50ms"] for a in DIRECT_ARMS)
    sol_none_chat, sol_none_resp = by[("gpt-5.6-sol-dz", "chat")], by[("gpt-5.6-sol-dz", "resp")]
    luna_none_chat, luna_none_resp = by[("gpt-5.6-luna-dz", "chat")], by[("gpt-5.6-luna-dz", "resp")]

    load_tbl = []
    for r in load_rows:
        load_tbl.append([r["arm"], f"≤{r['level']}", f"{r['ok']}/{r['requests']}", r["errors_429"], f"{r['wall_s']:.1f}",
                         f"{r['share_of_wall_at_target']:.0%}", f"{r['requests_per_s']:.2f}",
                         f"{r['aggregate_output_tok_per_s']:.0f}", f"{r['aggregate_visible_tok_per_s']:.0f}",
                         f"{ms(r['ttft_p50_ms'])} / {ms(r['ttft_p90_ms'])}", f"{ms(r['e2e_p50_ms'])} / {ms(r['e2e_p90_ms'])}"])
    total_429 = sum(r["errors_429"] for r in load_rows)
    total_other = sum(r["other_errors"] for r in load_rows)
    router_served = " / ".join(sorted({f for r in load_rows if r["arm"] == "router-sol-luna-balanced" for f in r["served_families"].split("|") if f}))
    router_reasoning = [r["reasoning_share_of_output"] for r in load_rows if r["arm"] == "router-sol-luna-balanced"]
    others_reasoning = max(r["reasoning_share_of_output"] for r in load_rows if r["arm"] != "router-sol-luna-balanced")
    l16_share = {r["arm"]: r["share_of_wall_at_target"] for r in load_rows if r["level"] == 16}
    l4_share = [r["share_of_wall_at_target"] for r in load_rows if r["level"] == 4]

    def lvl(arm, level, key):
        return next(r for r in load_rows if r["arm"] == arm and r["level"] == level)[key]

    judge_tbl = [[r["task"], r["arm"], f"{r['v1_mean']:.3f}", f"{r['v2_mean']:.3f}", f"{r['delta']:+.3f}", f"{r['v1_share_5']:.0%} → {r['v2_share_5']:.0%}"] for r in arm_rows]
    pb, pa = per_task["B"], per_task["A"]
    s03, s04 = pb["action_spreads"]["S03"], pb["action_spreads"]["S04"]
    if s03["v2"][0] != s03["v2"][1]:
        raise ValueError("S03 v2 scores are not uniform; the README sentence about S03 would be false.")
    s04_low = [s for s in s04["v2_scores"] if s < 5]
    if not s04_low or len(s04_low) == len(s04["v2_scores"]):
        raise ValueError("S04 v2 distribution no longer matches the README wording.")

    blocks = [
        t("# Lenovo Qira — Follow-up: API-path latency, stepped concurrency, judge recalibration",
          "# Lenovo Qira — 跟进实验：API 路径时延、阶梯并发、评分规则校准"),
        "",
        t("Three paid follow-up runs answer the questions the first two studies left open: whether the whole-answer bursts seen on the Chat Completions direct baselines were an API-path artefact, how the three candidate models and the balanced router behave at 4/8/16 concurrent requests, and how much the blind judge's scores move once the rubric states explicitly that the assistant has no tools. Same Sweden Central VM and resource, no search or tools, hardened harness (no silent SDK retries, usage required).",
          "三组付费跟进实验回答前两份研究留下的问题：Chat Completions 直连基线上看到的“整段一次到达”是否是 API 路径造成的；三个候选模型与 balanced Router 在 4/8/16 并发下的表现；以及评分规则明确写出“助手没有工具”之后，盲评分数会变动多少。同一台 Sweden Central VM、同一资源、不带搜索或工具，使用加固后的 harness（关闭 SDK 静默重试、必须收到 usage）。"),
        "",
        t("> Author: **Xinyu Wei (魏新宇)** · 2026-09-10 · runs `direct_" + run_direct + "`, `loadtest_" + run_load + "`, `quality_v2_*`",
          "> 作者: **Xinyu Wei (魏新宇)** · 2026-09-10 · 运行 `direct_" + run_direct + "`、`loadtest_" + run_load + "`、`quality_v2_*`"),
        "",
        "[English](README.md) | [中文](README-CN.md)",
        "",
        t("Related: [scenario benchmark](../qira-scenario-model-benchmark/README.md) · [router validation](../qira-model-router-validation/README.md)",
          "相关：[场景基准](../qira-scenario-model-benchmark/README-CN.md) · [Router 验证](../qira-model-router-validation/README-CN.md)"),
        "",
        t(f"## 1. Same deployments, two API paths: on Chat the first token arrived with the last ({chat_bursts}/564); on Responses it did not ({resp_bursts}/564)",
          f"## 1. 同一批部署、两条 API 路径：Chat 上首个 token 与最后一个同时到达（{chat_bursts}/564），Responses 上没有（{resp_bursts}/564）"),
        "",
        t(f"`gpt-5.6-sol-dz` and `gpt-5.6-luna-dz` (DataZoneStandard) were re-run on the **Responses API** with the identical 47 prompts, efforts and 1 + 3 iterations. On the Chat path {chat_bursts}/564 measured answers arrived in one burst (<50 ms first-to-last delta); on the Responses path **{resp_bursts}/564** did. TTFT/E2E are client-observed; the two paths were measured on different days, so absolute latency also carries a time-of-run effect.",
          f"`gpt-5.6-sol-dz` 与 `gpt-5.6-luna-dz`（DataZoneStandard）用完全相同的 47 题、effort 和 1+3 次迭代，在 **Responses API** 上重跑。Chat 路径有 {chat_bursts}/564 次测量回答整段一次到达（首末文本块间隔 <50 ms）；Responses 路径为 **{resp_bursts}/564**。TTFT/E2E 均为客户端观测值；两条路径在不同日期测量，绝对时延还叠加了时段效应。"),
        "",
        table("Arm|API path|TTFT P50 / P90 s|E2E P50 / P90 s|TPOT P50 ms|tok/s P50|Bursts <50 ms|Output tok|USD / 1,000",
              "实验组|API 路径|TTFT P50 / P90 s|E2E P50 / P90 s|TPOT P50 ms|tok/s P50|首末<50 ms|输出 token|美元 / 1,000 次", direct_tbl),
        "",
        t(f"- Direct Sol, effort not sent: TTFT P50 **{ms(sol_none_chat['ttft_p50_ms'])} s on Chat vs {ms(sol_none_resp['ttft_p50_ms'])} s on Responses**; the Chat E2E P50 ({ms(sol_none_chat['e2e_p50_ms'])} s) was within {abs(sol_none_chat['e2e_p50_ms'] - sol_none_chat['ttft_p50_ms']):.0f} ms of its TTFT — the first token arrived together with the last one. Direct Luna: {ms(luna_none_chat['ttft_p50_ms'])} s vs {ms(luna_none_resp['ttft_p50_ms'])} s. Taking the Responses TTFT as the model-side component, the delivery share of the Chat TTFT ranges from {min(1 - by[(a, 'resp')]['ttft_p50_ms'] / by[(a, 'chat')]['ttft_p50_ms'] for a in DIRECT_ARMS):.0%} to {max(1 - by[(a, 'resp')]['ttft_p50_ms'] / by[(a, 'chat')]['ttft_p50_ms'] for a in DIRECT_ARMS):.0%} across the four arms — large, but not uniformly \"most\" of it, and confounded by the different measurement days.",
          f"- 直连 Sol、不发 effort：TTFT P50 **Chat {ms(sol_none_chat['ttft_p50_ms'])} s vs Responses {ms(sol_none_resp['ttft_p50_ms'])} s**；Chat 路径的 E2E P50（{ms(sol_none_chat['e2e_p50_ms'])} s）与其 TTFT 只差 {abs(sol_none_chat['e2e_p50_ms'] - sol_none_chat['ttft_p50_ms']):.0f} ms——首个 token 和最后一个 token 一起到达。直连 Luna：{ms(luna_none_chat['ttft_p50_ms'])} s vs {ms(luna_none_resp['ttft_p50_ms'])} s。若把 Responses 的 TTFT 当作模型侧部分，Chat TTFT 中的交付占比在四个组里为 {min(1 - by[(a, 'resp')]['ttft_p50_ms'] / by[(a, 'chat')]['ttft_p50_ms'] for a in DIRECT_ARMS):.0%} 到 {max(1 - by[(a, 'resp')]['ttft_p50_ms'] / by[(a, 'chat')]['ttft_p50_ms'] for a in DIRECT_ARMS):.0%}——占比很大，但并非在每个组都“占大多数”，且受不同测量日期的混杂。"),
        t(f"- On the Responses path the per-token pace is measurable: Sol {sol_none_resp['tpot_p50_ms']:.1f} ms/token ({sol_none_resp['tok_per_s_p50']:.0f} tok/s), Luna {luna_none_resp['tpot_p50_ms']:.1f} ms/token ({luna_none_resp['tok_per_s_p50']:.0f} tok/s). The Chat-path `decode_tps` of ~10,000 in the router study was the burst, as that report already stated.",
          f"- Responses 路径下逐 token 节奏可以测：Sol {sol_none_resp['tpot_p50_ms']:.1f} ms/token（{sol_none_resp['tok_per_s_p50']:.0f} tok/s），Luna {luna_none_resp['tpot_p50_ms']:.1f} ms/token（{luna_none_resp['tok_per_s_p50']:.0f} tok/s）。Router 研究里 Chat 路径约 10,000 的 `decode_tps` 是整段到达造成的，该报告当时已经说明。"),
        t("- **Consequence for the router study:** the router arms (Chat path, GlobalStandard) and the direct baselines (Chat path, DataZoneStandard) both carried this delivery behaviour to different degrees (router arms ≈ 20–25 % bursts, direct ≈ 90 %). Paired TTFT differences between them remain uninterpretable as router overhead; that report already refuses that reading. Whether the burst comes from the DataZone serving path or from the Chat Completions streaming layer is **still not isolated** — this run changed the API only.",
          "- **对 Router 研究的影响：**Router 组（Chat 路径、GlobalStandard）和直连基线（Chat 路径、DataZoneStandard）都不同程度带有这种交付行为（Router 组约 20–25% 整段到达，直连约 90%）。两者之间的配对 TTFT 差值仍不能解读为 Router 开销；该报告已拒绝这种解读。整段到达究竟来自 DataZone 服务路径还是 Chat Completions 流式层，**仍未隔离**——本轮只换了 API。"),
        "",
        t("## 2. Stepped concurrency: 48-request batches with ≤ 1 / 4 / 8 / 16 in flight",
          "## 2. 阶梯并发：每批 48 次请求，并发上限 1 / 4 / 8 / 16"),
        "",
        t(f"{REQUESTS_PER_LEVEL} requests per level, cycling the 17 Qira prompts, submitted to a thread pool of <level> workers; one warm-up per arm excluded; levels run in ascending order with a 10 s settle between them; arms run one after another. Measurement code is `harness.run_one` unchanged. **This is a closed batch, not a steady-state load:** in-flight equals the level only until the queue drains, then decays to zero while the longest answers finish. The wall-clock (and therefore every aggregate below) includes that drain; the *share of wall at target* column, measured from the per-request start/finish offsets, says how much of each window actually ran at the stated concurrency. `gpt-4o-mini-bench`, `gpt-5-mini`, `gpt-5.6-luna` are GlobalStandard capacity 1000 on the Responses API; `router-sol-luna-balanced` is GlobalStandard capacity 300 on Chat Completions.",
          f"每级 {REQUESTS_PER_LEVEL} 次请求，循环使用 17 道 Qira 题，提交到 <level> 个工作线程的线程池；每组 1 次预热不计；级别升序执行，级间停 10 s；各组依次运行。测量代码仍是未修改的 `harness.run_one`。**这是封闭批次，不是稳态负载：**并发只在队列排空前等于目标级别，之后随最长回答收尾逐渐降到零。墙钟时间（因此下表全部聚合值）都包含这段收尾；“达到目标并发的时间占比”一列由逐请求的起止偏移量算出，说明每个窗口真正处于所标并发的时间比例。`gpt-4o-mini-bench`、`gpt-5-mini`、`gpt-5.6-luna` 为 GlobalStandard、capacity 1000、Responses API；`router-sol-luna-balanced` 为 GlobalStandard、capacity 300、Chat Completions。"),
        "",
        table("Arm|In flight|Completed|429|Wall s|Share of wall at target|req/s|Agg out tok/s (incl. reasoning)|Agg visible tok/s|TTFT P50 / P90 s|E2E P50 / P90 s",
              "实验组|并发上限|完成|429|墙钟 s|达到目标并发的时间占比|req/s|聚合输出 tok/s（含推理）|聚合可见 tok/s|TTFT P50 / P90 s|E2E P50 / P90 s", load_tbl),
        "",
        t(f"- **{total_429} requests returned 429 and {total_other} other errors across {len(load_rows) * REQUESTS_PER_LEVEL} measured requests.** With `max_retries=0`, every one would have been recorded. At capacity 1000 (≈1M TPM) and 16 concurrent short prompts, this test does not reach the PAYGO rate limit; the 429 onset is a function of the deployment's capacity setting, not of the model, and was not found here.",
          f"- **{len(load_rows) * REQUESTS_PER_LEVEL} 次测量请求中，{total_429} 次返回 429，{total_other} 次其他错误。**`max_retries=0` 下每一次都会被记录。在 capacity 1000（约 1M TPM）、16 并发短提示词下，本测试没有触及 PAYGO 限流；429 出现点取决于部署的 capacity 设置而不是模型，本轮未观测到。"),
        t(f"- **Do not read the ≤16 rows as \"throughput at 16 in flight\".** At ≤16 the target concurrency held for only {min(l16_share.values()):.0%}–{max(l16_share.values()):.0%} of the wall (≤4: {min(l4_share):.0%}–{max(l4_share):.0%}); the rest of each window is a few long WFM01 answers draining alone, so the aggregate tok/s at ≤16 is dominated by how long each model's longest answer is. A steady-state figure needs a fixed-duration window with continuous refill, or ≥10× level requests per level — neither was run. TTFT/E2E percentiles are far less affected: every request started at a moment when in-flight had just refilled to the level.",
          f"- **不要把 ≤16 这几行读成“16 并发下的吞吐”。**≤16 时目标并发只维持了墙钟的 {min(l16_share.values()):.0%}–{max(l16_share.values()):.0%}（≤4 时为 {min(l4_share):.0%}–{max(l4_share):.0%}）；窗口其余时间是少数几条很长的 WFM01 回答在单独收尾，因此 ≤16 的聚合 tok/s 主要由各模型最长回答的长度决定。要得到稳态数字，需要固定时长、持续补充请求的窗口，或每级 ≥10 倍于并发数的请求量——两者本轮都没有做。TTFT/E2E 分位数受影响小得多：每个请求都是在并发刚补满到目标级别时开始的。"),
        t(f"- `router-sol-luna-balanced` served **{router_served}** for all {sum(r['ok'] for r in load_rows if r['arm'] == 'router-sol-luna-balanced')} completed load-test requests (17 Qira prompts), consistent with the router study where balanced never selected Sol for a Qira prompt. Because no effort was sent, the router-served Luna ran with default reasoning ({min(router_reasoning):.0%}–{max(router_reasoning):.0%} of its output tokens are reasoning tokens) while the three `@none`/`@minimal`/non-reasoning arms had {others_reasoning:.0%}: compare the router on the *visible* tok/s column, not the one including reasoning. Its per-request tok/s (in the CSV) also carries the Chat-path delivery effect ({sum(r['bursts_lt50ms'] for r in load_rows if r['arm'] == 'router-sol-luna-balanced')}/192 bursts); wall-clock aggregates are unaffected by bursts.",
          f"- `router-sol-luna-balanced` 在全部 {sum(r['ok'] for r in load_rows if r['arm'] == 'router-sol-luna-balanced')} 次完成的压测请求（17 道 Qira 题）中均服务 **{router_served}**，与 Router 研究中 balanced 从未为 Qira 题选择 Sol 一致。由于未发送 effort，Router 服务的 Luna 以默认推理运行（输出 token 中 {min(router_reasoning):.0%}–{max(router_reasoning):.0%} 是推理 token），而 `@none`/`@minimal`/非推理的三个组为 {others_reasoning:.0%}：比较 Router 时请看“聚合可见 tok/s”列，不要用含推理的那一列。其单请求 tok/s（见 CSV）还带有 Chat 路径的交付效应（{sum(r['bursts_lt50ms'] for r in load_rows if r['arm'] == 'router-sol-luna-balanced')}/192 次整段到达）；按墙钟计算的聚合值不受整段到达影响。"),
        "",
        t("## 3. Judge rubric v2 removes the S03 inconsistency; scores still move by tenths",
          "## 3. 评分规则 v2 消除了 S03 的不一致；分数仍有零点几分的波动"),
        "",
        t(f"Rubric v1 penalised some tool-less \"I cannot set a timer, use your clock app\" answers for not performing the action and rewarded others for stating the limitation: on S03 the same behaviour received instruction_following {s03['v1'][0]}–{s03['v1'][1]} across the ten arms. Rubric v2 (`{judge.RUBRIC_VERSION}`) states that the assistant has no tools and that plainly stating the limitation plus the shortest correct manual path fully satisfies the request. Every Task B (470) and Task A (187) answer was re-judged by the same `judge-terra` deployment; the judged iteration is identical to v1 in every cell.",
          f"规则 v1 对部分“我不能设置计时器，请用时钟应用”这类无工具回答按“没有执行动作”扣分，对另一部分又因“如实说明限制”给高分：S03 上同一行为在十个组的 instruction_following 从 {s03['v1'][0]} 到 {s03['v1'][1]}。规则 v2（`{judge.RUBRIC_VERSION}`）明确写出助手没有工具，如实说明限制并给出最短正确路径即完全满足请求。全部 Task B（470）和 Task A（187）回答由同一 `judge-terra` 部署重评，每个单元格评的迭代与 v1 相同。"),
        "",
        t(f"- **S03 (set a timer):** v2 scores all ten arms {s03['v2'][0]}/5 on instruction_following — the identified inconsistency is gone.\n- **S04 (open Bluetooth settings):** {len(s04['v2_scores']) - len(s04_low)} answers scored 5; the {len(s04_low)} that scored {' and '.join(str(s) for s in s04_low)} are the ones whose v2 justification records that the answer gave the path without stating it could not open the page. That is the rule applied as written, with a {max(s04_low) - min(s04_low)}-point spread between two near-identical answers remaining as judge noise.",
          f"- **S03（设置计时器）：**v2 下十个组的 instruction_following 全部为 {s03['v2'][0]}/5——已确认的不一致消失。\n- **S04（打开蓝牙设置）：**{len(s04['v2_scores']) - len(s04_low)} 条回答得 5 分；得 {' 和 '.join(str(s) for s in s04_low)} 分的 {len(s04_low)} 条，其 v2 评语记录的是“只给路径、没说明无法打开”。这是规则按字面执行的结果，两条几乎相同的回答之间仍有 {max(s04_low) - min(s04_low)} 分差距，属于残余的评分噪声。"),
        "",
        table("Task|Arm|v1 mean|v2 mean|Δ|share of 5.0 (v1 → v2)", "任务|实验组|v1 均分|v2 均分|Δ|满分占比 (v1 → v2)", judge_tbl),
        "",
        t(f"- Task B: {pb['cells_changed']}/{pb['cells']} cells changed, mean |Δ| {pb['mean_abs_delta']:.3f}; Task A: {pa['cells_changed']}/{pa['cells']} cells changed, mean |Δ| {pa['mean_abs_delta']:.3f}. Arm means move by up to {max(abs(r['delta']) for r in arm_rows):.2f} and several orderings change (v1 also contains exact ties, e.g. two Task B arms at 4.9149): differences of a few hundredths between arms are inside the judge's own variance and must not drive a choice. Full means and both rankings: [judge_rubric_comparison_arms.csv](outputs/judge_rubric_comparison_arms.csv).",
          f"- Task B：{pb['cells_changed']}/{pb['cells']} 个单元格分数变化，平均 |Δ| {pb['mean_abs_delta']:.3f}；Task A：{pa['cells_changed']}/{pa['cells']} 个变化，平均 |Δ| {pa['mean_abs_delta']:.3f}。各组均分最多变动 {max(abs(r['delta']) for r in arm_rows):.2f}，多处排序互换（v1 里还存在完全同分，例如 Task B 两组同为 4.9149）：组间零点零几分的差距落在裁判自身波动之内，不能作为选型依据。完整均分与两版排序见 [judge_rubric_comparison_arms.csv](outputs/judge_rubric_comparison_arms.csv)。"),
        t("- Both rubrics are LLM-as-a-judge without a gold answer; v2 removes one identified inconsistency, it does not make the scores a measure of user-perceived quality. Per-question deltas: [judge_rubric_comparison_questions.csv](outputs/judge_rubric_comparison_questions.csv).",
          "- 两个版本都是没有标准答案的 LLM 评分；v2 消除了一处已确认的不一致，并不使分数成为用户感知质量的度量。逐题差值见 [judge_rubric_comparison_questions.csv](outputs/judge_rubric_comparison_questions.csv)。"),
        "",
        t("## 4. What changed in the runnable code (also applied in the router folder)", "## 4. 可运行代码的变更（已同步进 Router 目录）"),
        "",
        t("- `harness.py`: SDK `max_retries=0`; a stream that ends without usage is `missing_usage`, not a zero-cost success; Chat `finish_reason` retained and anything other than `stop`/`length` is an error.\n- `analyze.py`: the router report applies the same completed/non-truncated/usage-present filter as the direct report and exits on mixed environments.\n- `judge.py`: rubric v2 with tool-less rule, `rubric_version` on every row, refuses to overwrite an existing score file, keeps 2 retries (it is not a latency measurement).\n- `loadtest.py`: new; stepped concurrency on top of `harness.run_one`.",
          "- `harness.py`：SDK `max_retries=0`；没有 usage 的流记为 `missing_usage`，不算零成本成功；保留 Chat `finish_reason`，非 `stop`/`length` 一律记错。\n- `analyze.py`：Router 报告与直连报告使用同一套“已完成 / 未截断 / 有 usage”过滤，混合环境直接退出。\n- `judge.py`：规则 v2 加入无工具规则，每行带 `rubric_version`，拒绝覆盖已有评分文件，保留 2 次重试（评分不是时延测量）。\n- `loadtest.py`：新增，在 `harness.run_one` 之上做阶梯并发。"),
        "",
        t("## 5. Reproduce", "## 5. 复现"),
        "",
        "```bash",
        "# offline: rebuild every CSV and both READMEs from the retained evidence",
        "python scripts/build_followup_report.py",
        "python -m unittest discover -s tests",
        "",
        "# live (paid): same-region Linux VM, Entra identity with Cognitive Services OpenAI User",
        "export AZURE_OPENAI_ENDPOINT=\"https://<RESOURCE_NAME>.openai.azure.com/\"",
        "python harness.py --mode direct --api responses --dataset datasets/router_taskb.jsonl \\",
        "  --deployments gpt-5.6-sol-dz,gpt-5.6-luna-dz --region swedencentral --client-location swedencentral-linux-vm \\",
        "  --efforts=-,low --iterations 3 --warmup 1",
        "python loadtest.py --dataset datasets/qira_scenarios.jsonl \\",
        "  --arms \"gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat\" \\",
        "  --levels 1,4,8,16 --requests-per-level 48 --region swedencentral --client-location swedencentral-linux-vm",
        "python judge.py outputs/<run>.jsonl --judge-deployment judge-terra --dataset datasets/<dataset>.jsonl --out outputs/quality_v2_<run>.jsonl",
        "```",
        "",
        t("## 6. Evidence", "## 6. 证据"),
        "",
        t(f"- Full answers: `outputs/raw_fulltext/direct_{run_direct}.jsonl` (752 records), `outputs/raw_fulltext/loadtest_{run_load}.jsonl`; every record keeps `response_sha256`.\n- `outputs/loadtest_{run_load}.summary.json` written live by `loadtest.py`; the builder recomputes every aggregate from the records and refuses to publish on disagreement.\n- `outputs/quality_v2_{RUN_B}.jsonl`, `outputs/quality_v2_{RUN_A}.jsonl`; v1 inputs and the historical Chat-path direct requests are under `outputs/inputs/` with [manifest.json](outputs/inputs/manifest.json).\n- Costs are Global list-price estimates by served model (DataZone premium and router fee excluded); this is not an invoice. Resource state after the run: [resource_closeout_followup.json](outputs/resource_closeout_followup.json).",
          f"- 全文回答：`outputs/raw_fulltext/direct_{run_direct}.jsonl`（752 条）、`outputs/raw_fulltext/loadtest_{run_load}.jsonl`；每条记录保留 `response_sha256`。\n- `outputs/loadtest_{run_load}.summary.json` 由 `loadtest.py` 实时写出；生成器从原始记录重算全部聚合值，不一致即拒绝生成。\n- `outputs/quality_v2_{RUN_B}.jsonl`、`outputs/quality_v2_{RUN_A}.jsonl`；v1 输入与历史 Chat 路径直连记录位于 `outputs/inputs/`，见 [manifest.json](outputs/inputs/manifest.json)。\n- 成本为按实际服务模型的 Global 牌价估算（不含 DataZone 溢价与路由费），不是账单。运行后的资源状态见 [resource_closeout_followup.json](outputs/resource_closeout_followup.json)。"),
        "",
    ]
    (ROOT / "README.md").write_text(render("en", blocks), encoding="utf-8", newline="\n")
    (ROOT / "README-CN.md").write_text(render("zh", blocks), encoding="utf-8", newline="\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if the READMEs would change")
    args = parser.parse_args()
    validate_input_manifest()
    registry = analyze.load_registry(ROOT / "config" / "models.json")
    pricing = analyze.load_pricing(ROOT / "config" / "pricing.json")
    before = {n: (ROOT / n).read_text(encoding="utf-8") if (ROOT / n).exists() else None for n in ("README.md", "README-CN.md")}

    run_direct, direct_records, direct_rows = direct_comparison(pricing, registry)
    run_load, load_records, load_rows = load_test()
    arm_rows, question_rows, per_task = rubric_comparison()
    provenance = {
        "runs": {"direct": run_direct, "loadtest": run_load, "judge_v2": [RUN_B, RUN_A]},
        "rubric_version": judge.RUBRIC_VERSION,
        "vm_transfer_sha256_before_redaction": {
            f"direct_{run_direct}.jsonl.xz": "21e9eb8a4d80536cfa9f4cd4f8eb1aa486b7a054131856c1ea46ca7ed23a6b08",
            f"loadtest_{run_load}.jsonl.xz": "e58726b02c993e749625097ec8dbb798db0ceed88eed6c88bd75e896518cd74a",
            f"loadtest_{run_load}.summary.json": "4b7199d828b177cd9efa98aa0b02f65f484d54fc75da81bbbf6184360c74905f",
            "followup_judge_v2.tar.xz": "a84c1fa6d857e15c8f01a73ae803c57ea9d5aad3c4ad376593cea04f5760c115",
        },
        "redaction": "endpoint_host replaced by YOUR-ENDPOINT.cognitiveservices.azure.com in the raw records, summary and VM logs after transfer; response_sha256 hashes response_text only and is unaffected.",
        "sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): sha(p) for p in sorted(OUTPUT.rglob("*")) if p.is_file() and p.name != "provenance_followup.json"},
    }
    (OUTPUT / "provenance_followup.json").write_text(json.dumps(provenance, indent=2) + "\n", encoding="utf-8", newline="\n")
    build_readmes(run_direct, direct_rows, run_load, load_rows, arm_rows, per_task, provenance)
    after = {n: (ROOT / n).read_text(encoding="utf-8") for n in before}
    if args.check and any(before[n] != after[n] for n in before):
        raise SystemExit("README drift: regenerate and commit.")
    print(f"VERIFIED: direct re-measure {len(direct_records)} rows, load test {len(load_records)} records, "
          f"judge v2 {sum(1 for r in question_rows)} cells; {len(direct_rows) + len(load_rows) + len(arm_rows)} summary rows written.")


if __name__ == "__main__":
    main()
