"""Validate the Task B (Model Router) evidence archive and produce auditable per-request reports.

Input : outputs/evidence_router_<run>.json.xz   (written on the VM by scripts/export_router_evidence.py)
Output: outputs/router_<run>.metrics.jsonl        numerical per-request records + list-price cost
        outputs/quality_router_<run>.jsonl        blind judge scores (copied out of the archive)
        outputs/provenance_router_<run>.json
        outputs/router_arm_summary.csv            one row per arm (3 router modes x 2 efforts + 2 direct x 2 efforts)
        outputs/router_routing_by_tier.csv        served-model share per arm x tier
        outputs/router_routing_by_category.csv    served-model share per arm x tier x category
        outputs/router_qira_scenarios.csv         all 10 arms x six Qira scenarios
        outputs/router_question_hits.csv          per arm x question: Sol hit rate over the measured iterations
        outputs/router_overhead.csv               routed-to-F TTFT minus direct-F TTFT, paired per question
        outputs/router_single_turn_sessions.csv   every measured request
        outputs/Qira-Router-实测结果-<date>.md   Chinese narrative report generated from the same numbers

Every check below raises instead of printing a warning: a report that cannot
prove its matrix is complete and single-environment must not be produced.

Author: Xinyu Wei (魏新宇)
"""

import argparse
import csv
import hashlib
import json
import lzma
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import analyze  # noqa: E402

RUN = "20260909_223737"
REPORT_DATE = "20260910"
OUTPUT = ROOT / "outputs"
STRONG, CHEAP = "gpt-5.6-sol", "gpt-5.6-luna"
ROUTER_DEPLOYMENTS = ("router-sol-luna-balanced", "router-sol-luna-cost", "router-sol-luna-quality")
DIRECT_DEPLOYMENTS = {"gpt-5.6-sol-dz": STRONG, "gpt-5.6-luna-dz": CHEAP}
EFFORTS = (None, "low")          # None = reasoning_effort not sent
ITERATIONS = 4                   # 1 warm-up + 3 measured
TIERS = ("simple", "moderate", "complex")
MODE_OF = {"router-sol-luna-balanced": "balanced", "router-sol-luna-cost": "cost", "router-sol-luna-quality": "quality"}
RUN_SOURCES = ("harness.py", "judge.py", "analyze.py", "config/models.json", "config/pricing.json",
               "datasets/router_taskb.jsonl", "scripts/build_router_dataset.py")
PUBLIC_SOURCE_SHA256 = {
    "harness.py": (
        "72ff098339ad9f21e17a6a532e5cbba0cd54113010377d24503e903a9ea24453",
        "995eebb5b2a5c3a34d4fda6aacfac7dac2529ddca89b7eb435e0dc40ba04c211",
    ),
    "analyze.py": (
        "e45debeab571b4dc8a4b63711c860371ff95886ab8822869e3de010f653f3a91",
        "1cedc02e7e96f652ea7041be216702de4ffea0c4182e6c5a9a7616e21b16327f",
    ),
    "config/models.json": (
        "364369d7a90c229a3fd2363176d728740f63321d54598268f74d11e6722946ff",
        "18aed9fede9f2d969c4b40fcab7b785e7b20b9eb132fc98a4b8260a28d4d8747",
    ),
    "datasets/router_taskb.jsonl": (
        "0e002a575c180c91da6cf6c98251a8faec0562ae1edba96b6e6f8c228185df54",
        "1cf7d872ea8b9506b466d83eb543614d0ac6988b69efe7c3cfd5bcd07cfc0ebd",
    ),
}
ARCHIVE_SHA256 = {
    "37e5dfa30117d687f175ed4bcae748bb4bba432484bdd493324cba7cd45c4061":
        "public (transcript prompts withheld, numbers unchanged)",
}


def arm_name(deployment: str, effort: str | None) -> str:
    return f"{deployment}@{effort}" if effort else deployment


def mean(rows, field):
    return statistics.mean(r[field] for r in rows)


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def share(rows, family):
    return sum(1 for r in rows if r["served_model_family"] == family) / len(rows) if rows else None


def load_evidence(archive: Path):
    payload = archive.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if digest not in ARCHIVE_SHA256:
        raise ValueError(
            f"Evidence archive SHA256 {digest} does not match any pinned checksum.")
    evidence = json.loads(lzma.decompress(payload))
    if evidence.get("task") != "B-model-router" or evidence.get("run_id") != RUN:
        raise ValueError("Archive is not the Task B run this builder was written for.")
    if not evidence.get("complete"):
        raise ValueError("Archive is marked partial; export again after the harness and judge have finished.")
    if evidence["provenance"]["vm_metadata"]["location"].casefold() != "swedencentral":
        raise ValueError("IMDS does not confirm the benchmark VM region.")
    records = [dict(zip(evidence["performance_columns"], values, strict=True)) for values in evidence["performance_values"]]
    return evidence, records, evidence["quality"], digest


def validate(records, quality, dataset):
    expected_ids = {item["id"] for item in dataset}
    if len(dataset) != 47 or len(expected_ids) != 47:
        raise ValueError("The fixed Task B dataset must contain 47 distinct questions.")
    if Counter(item["tier"] for item in dataset) != Counter(simple=15, moderate=18, complex=14):
        raise ValueError("The fixed Task B complexity matrix has changed.")
    tier_of = {item["id"]: item["tier"] for item in dataset}
    expected_arms = {arm_name(d, e) for d in (*ROUTER_DEPLOYMENTS, *DIRECT_DEPLOYMENTS) for e in EFFORTS}
    expected_keys = {(a, q, i) for a in expected_arms for q in expected_ids for i in range(1, ITERATIONS + 1)}
    actual_keys = [(r["arm"], r["question_id"], r["iteration"]) for r in records]
    n_expected = len(expected_arms) * len(expected_ids) * ITERATIONS
    if len(records) != n_expected or len(set(actual_keys)) != n_expected or set(actual_keys) != expected_keys:
        raise ValueError(f"Missing, duplicated, or unexpected matrix cells ({len(records)} rows, expected {n_expected}).")

    conditions = {(r["region"], r["endpoint_host"], r["deployment_type"], r["client_location"], r["api"]) for r in records}
    if len(conditions) != 1:
        raise ValueError(f"Mixed test environments: {conditions}")
    if next(iter(conditions))[4] != "chat":
        raise ValueError("Task B must use Chat Completions for every arm (the tested AOAI Responses path rejected the router).")
    if next(iter(conditions))[0].casefold() != "swedencentral":
        raise ValueError("Measurement region differs from the verified VM region.")

    for r in records:
        if r["run_id"] != RUN or r["reasoning_effort"] not in EFFORTS:
            raise ValueError("Unexpected run ID or effort in a measurement.")
        if r["arm"] != arm_name(r["model_requested"], r["reasoning_effort"]):
            raise ValueError("Arm label does not match the requested deployment and effort.")
        if r["status"] != "completed" or r["error"] or r["truncated"] or r["tools_enabled"] is not False:
            raise ValueError(f"Invalid measurement: {r['arm']}/{r['question_id']}/{r['iteration']}")
        if r["warmup"] != (r["iteration"] == 1):
            raise ValueError("Incorrect warmup flag.")
        if not 0 <= r["cached_tokens"] <= r["prompt_tokens"] or not 0 <= r["reasoning_tokens"] <= r["completion_tokens"]:
            raise ValueError("Inconsistent token accounting.")
        if r["prompt_tokens"] == 0 or r["completion_tokens"] == 0:
            raise ValueError(f"Record without usage cannot be costed: {r['arm']}/{r['question_id']}/{r['iteration']}")
        if r["response_chars"] <= 0 or r["ttft_ms"] is None or not 0 <= r["ttft_ms"] <= r["e2e_ms"]:
            raise ValueError("Empty or inconsistent timing record.")
        if r["complexity_tag"] != tier_of[r["question_id"]]:
            raise ValueError("Tier label in the record differs from the dataset.")
        deployment = r["model_requested"]
        if deployment in ROUTER_DEPLOYMENTS:
            if r["router_mode"] != MODE_OF[deployment] or not r["router_attempts"] or r["served_model_family"] not in (STRONG, CHEAP):
                raise ValueError(f"Router record without a usable routing trace: {r['arm']}/{r['question_id']}")
            if r["model_actually_served"] and not r["model_actually_served"].startswith(r["served_model_family"]):
                raise ValueError("served_model_family does not match model_actually_served.")
            successful = [a for a in r["router_attempts"] if a.get("status") == 200]
            if len(successful) != 1 or successful[-1].get("model") != r["served_model_family"]:
                raise ValueError(f"Routing trace disagrees with the served model: {r['arm']}/{r['question_id']}")
        elif deployment in DIRECT_DEPLOYMENTS:
            if r["served_model_family"] != DIRECT_DEPLOYMENTS[deployment] or r["router_mode"]:
                raise ValueError(f"Direct baseline served an unexpected model: {r['arm']}/{r['question_id']}")
        else:
            raise ValueError(f"Unknown deployment in run: {deployment}")
    for qid in expected_ids:
        if len({r["max_output_tokens"] for r in records if r["question_id"] == qid}) != 1:
            raise ValueError("Output cap differs across arms for the same question.")

    qmap = {(q["arm"], q["question_id"]): q for q in quality}
    if len(quality) != len(qmap) or set(qmap) != {(a, q) for a in expected_arms for q in expected_ids}:
        raise ValueError(f"Incomplete quality coverage ({len(quality)} rows).")
    measured = {(r["arm"], r["question_id"], r["iteration"]): r for r in records if not r["warmup"]}
    for q in quality:
        if q["run_id"] != RUN:
            raise ValueError("Quality score belongs to a different run.")
        if q.get("error") or any(type(q.get(d)) is not int or not 1 <= q[d] <= 5 for d in analyze.QUALITY_DIMS):
            raise ValueError(f"Invalid quality scores: {q['arm']}/{q['question_id']}: {q.get('error')}")
        if (q["arm"], q["question_id"], q["iteration"]) not in measured:
            raise ValueError("A judged answer does not correspond to a measured iteration.")
        if abs(q["quality_mean"] - statistics.mean(q[d] for d in analyze.QUALITY_DIMS)) > 1e-9:
            raise ValueError("Quality mean differs from its five dimension scores.")
    return expected_arms, expected_ids


def validate_sources(provenance):
    for name in RUN_SOURCES:
        expected = provenance["source_sha256"].get(name)
        snapshot = ROOT / "outputs" / "source_snapshot" / name
        source = snapshot if snapshot.is_file() else ROOT / name
        actual = hashlib.sha256(source.read_bytes()).hexdigest()
        if not expected or actual != expected:
            original, public = PUBLIC_SOURCE_SHA256.get(name, (None, None))
            if not (expected == original and actual == public):
                raise ValueError(f"Source differs from the executed run: {name}")


def main():
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args()
    evidence, records, quality, digest = load_evidence(
        OUTPUT / f"evidence_router_{RUN}.json.xz")
    validate_sources(evidence["provenance"])
    dataset = [json.loads(line) for line in (ROOT / "datasets" / "router_taskb.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    item_of = {item["id"]: item for item in dataset}
    expected_arms, expected_ids = validate(records, quality, dataset)

    registry = analyze.load_registry(ROOT / "config" / "models.json")
    pricing = analyze.load_pricing(ROOT / "config" / "pricing.json")
    analyze._REGISTRY = registry
    for r in records:
        # Cost is always billed at the SERVED model's rate, never the router's name.
        r["cost_usd"] = analyze.cost_for(pricing, r["served_model_family"], r["prompt_tokens"], r["cached_tokens"], r["completion_tokens"])
        if r["cost_usd"] is None:
            raise ValueError(f"Missing price for served model {r['served_model_family']}.")
        r["total_tokens"] = r["prompt_tokens"] + r["completion_tokens"]
        r["visible_output_tokens_estimate"] = r["completion_tokens"] - r["reasoning_tokens"]
        r["category"] = r.get("category") or item_of[r["question_id"]].get("category")
        r["source"] = item_of[r["question_id"]].get("source")

    provenance = dict(evidence["provenance"])
    provenance["archive_sha256"] = digest
    for name, rows in ((f"router_{RUN}.metrics.jsonl", records), (f"quality_router_{RUN}.jsonl", quality)):
        (OUTPUT / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    (OUTPUT / f"provenance_router_{RUN}.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    measured = [r for r in records if not r["warmup"]]
    qmap = {(q["arm"], q["question_id"]): q for q in quality}
    arms = sorted(expected_arms, key=lambda a: (a.split("@")[0] not in ROUTER_DEPLOYMENTS, a))
    router_arms = [a for a in arms if a.split("@")[0] in ROUTER_DEPLOYMENTS]
    direct_arms = [a for a in arms if a.split("@")[0] in DIRECT_DEPLOYMENTS]

    def effort_of(arm):
        return arm.split("@")[1] if "@" in arm else None

    # ---- direct policies keyed by (family, effort): what all-Sol / all-Luna would cost & score
    policy_cost, policy_quality, policy_ttft = {}, {}, {}
    for arm in direct_arms:
        group = [r for r in measured if r["arm"] == arm]
        key = (DIRECT_DEPLOYMENTS[arm.split("@")[0]], effort_of(arm))
        policy_cost[key] = mean(group, "cost_usd") * 1000
        policy_quality[key] = statistics.mean(qmap[(arm, q)]["quality_mean"] for q in expected_ids)
        policy_ttft[key] = analyze.pct([r["ttft_ms"] for r in group], 50)

    # ---- arm summary
    arm_rows = []
    for arm in arms:
        group = [r for r in measured if r["arm"] == arm]
        scores = [qmap[(arm, q)] for q in expected_ids]
        effort = effort_of(arm)
        rl = [r["router_latency_ms"] for r in group if r.get("router_latency_ms") is not None]
        row = {
            "arm": arm, "deployment": arm.split("@")[0], "router_mode": group[0].get("router_mode") or "direct",
            "reasoning_effort": effort or "not-sent", "requests": len(group),
            "sol_share": share(group, STRONG), "luna_share": share(group, CHEAP),
            "router_fallbacks": sum(1 for r in group if r.get("router_fallback")),
            "router_decision_ms_p50": analyze.pct(rl, 50) if rl else None,
            "router_decision_ms_p95": analyze.pct(rl, 95) if rl else None,
            "ttft_p50_ms": analyze.pct([r["ttft_ms"] for r in group], 50),
            "ttft_p90_ms": analyze.pct([r["ttft_ms"] for r in group], 90),
            "ttft_p95_ms": analyze.pct([r["ttft_ms"] for r in group], 95),
            "e2e_p50_ms": analyze.pct([r["e2e_ms"] for r in group], 50),
            "e2e_p90_ms": analyze.pct([r["e2e_ms"] for r in group], 90),
            "e2e_p95_ms": analyze.pct([r["e2e_ms"] for r in group], 95),
            # Client-observed inter-delta timing. On this run the direct baselines delivered
            # most answers in one burst, so these are delivery estimates, not GPU decode rates.
            "tpot_p50_ms": analyze.pct([r["tpot_ms"] for r in group if r["tpot_ms"] is not None], 50),
            "tpot_p90_ms": analyze.pct([r["tpot_ms"] for r in group if r["tpot_ms"] is not None], 90),
            "decode_tps_p50": analyze.pct([r["decode_tps"] for r in group if r["decode_tps"] is not None], 50),
            "decode_span_p50_ms": analyze.pct([r["decode_ms"] for r in group if r["decode_ms"] is not None], 50),
            "decode_span_lt50ms_requests": sum(r["decode_ms"] is not None and r["decode_ms"] < 50 for r in group),
            "input_tokens_mean": mean(group, "prompt_tokens"),
            "cached_tokens_mean": mean(group, "cached_tokens"),
            "output_tokens_mean": mean(group, "completion_tokens"),
            "reasoning_tokens_mean": mean(group, "reasoning_tokens"),
            "total_tokens_mean": mean(group, "total_tokens"),
            "cost_usd_sum": sum(r["cost_usd"] for r in group),
            "cost_usd_per_1000_requests": mean(group, "cost_usd") * 1000,
            "cost_vs_all_sol": None, "cost_vs_all_luna": None,
            "judge_quality_mean": statistics.mean(q["quality_mean"] for q in scores),
            "judge_quality_min": min(q["quality_mean"] for q in scores),
            "judge_n": len(scores),
            "quality_vs_all_sol": None, "quality_vs_all_luna": None,
        }
        for tier in TIERS:
            sub = [r for r in group if r["complexity_tag"] == tier]
            row[f"cost_per_1000_{tier}"] = mean(sub, "cost_usd") * 1000
            row[f"quality_{tier}"] = statistics.mean(qmap[(arm, q)]["quality_mean"] for q in expected_ids if item_of[q]["tier"] == tier)
        if (STRONG, effort) in policy_cost:
            row["cost_vs_all_sol"] = row["cost_usd_per_1000_requests"] / policy_cost[(STRONG, effort)] - 1
            row["quality_vs_all_sol"] = row["judge_quality_mean"] - policy_quality[(STRONG, effort)]
        if (CHEAP, effort) in policy_cost:
            row["cost_vs_all_luna"] = row["cost_usd_per_1000_requests"] / policy_cost[(CHEAP, effort)] - 1
            row["quality_vs_all_luna"] = row["judge_quality_mean"] - policy_quality[(CHEAP, effort)]
        arm_rows.append(row)

    # ---- routing share by tier / by category (router arms only)
    tier_rows, category_rows = [], []
    for arm in router_arms:
        group = [r for r in measured if r["arm"] == arm]
        for tier in (*TIERS, "ALL"):
            sub = group if tier == "ALL" else [r for r in group if r["complexity_tag"] == tier]
            tier_rows.append({"arm": arm, "tier": tier, "requests": len(sub),
                              "sol_requests": sum(1 for r in sub if r["served_model_family"] == STRONG),
                              "sol_share": share(sub, STRONG), "luna_share": share(sub, CHEAP),
                              "cost_usd_per_1000_requests": mean(sub, "cost_usd") * 1000,
                              "ttft_p50_ms": analyze.pct([r["ttft_ms"] for r in sub], 50)})
        cats = sorted({(r["complexity_tag"], r["category"]) for r in group}, key=lambda t: (TIERS.index(t[0]), t[1]))
        for tier, cat in cats:
            sub = [r for r in group if r["complexity_tag"] == tier and r["category"] == cat]
            category_rows.append({"arm": arm, "tier": tier, "category": cat, "source": sub[0]["source"],
                                  "questions": len({r["question_id"] for r in sub}), "requests": len(sub),
                                  "sol_requests": sum(1 for r in sub if r["served_model_family"] == STRONG),
                                  "sol_share": share(sub, STRONG)})

    scenario_rows = []
    scenarios = sorted({item["scenario"] for item in dataset if item["source"] == "qira_scenarios"})
    for arm in arms:
        for scenario in scenarios:
            ids = {item["id"] for item in dataset if item.get("scenario") == scenario}
            group = [r for r in measured if r["arm"] == arm and r["question_id"] in ids]
            scenario_rows.append({
                "arm": arm, "scenario": scenario, "questions": len(ids), "requests": len(group),
                "sol_requests": sum(r["served_model_family"] == STRONG for r in group),
                "sol_share": share(group, STRONG),
                "ttft_p50_ms": analyze.pct([r["ttft_ms"] for r in group], 50),
                "cost_usd_per_1000_requests": mean(group, "cost_usd") * 1000,
                "judge_quality_mean": statistics.mean(qmap[(arm, qid)]["quality_mean"] for qid in ids),
            })

    # ---- per-question hit rate (the deliverable: which model actually served each question, N=3)
    hit_rows = []
    for arm in router_arms:
        for qid in sorted(expected_ids):
            sub = [r for r in measured if r["arm"] == arm and r["question_id"] == qid]
            served = [r["served_model_family"] for r in sorted(sub, key=lambda r: r["iteration"])]
            warm = next(r["served_model_family"] for r in records if r["arm"] == arm and r["question_id"] == qid and r["warmup"])
            hit_rows.append({"arm": arm, "question_id": qid, "tier": item_of[qid]["tier"], "category": item_of[qid]["category"],
                             "source": item_of[qid]["source"], "measured_iterations": len(sub),
                             "sol_hits": served.count(STRONG), "sol_hit_rate": served.count(STRONG) / len(served),
                             "served_sequence": ">".join(s.replace("gpt-5.6-", "") for s in served),
                             "warmup_served": warm.replace("gpt-5.6-", ""),
                             "consistent": len(set(served)) == 1,
                             "judge_quality_mean": qmap[(arm, qid)]["quality_mean"],
                             "judged_iteration_served": qmap[(arm, qid)].get("model_actually_served")})

    # ---- router overhead: routed-to-F vs direct-F at the same effort, paired per question
    direct_ttft = defaultdict(list)
    for r in measured:
        if r["model_requested"] in DIRECT_DEPLOYMENTS:
            direct_ttft[(r["served_model_family"], r["reasoning_effort"], r["question_id"])].append(r["ttft_ms"])
    overhead_rows = []
    for arm in router_arms:
        for family in (STRONG, CHEAP):
            deltas = []
            for r in measured:
                if r["arm"] == arm and r["served_model_family"] == family:
                    base = direct_ttft.get((family, r["reasoning_effort"], r["question_id"]))
                    if base:
                        deltas.append(r["ttft_ms"] - statistics.median(base))
            if deltas:
                overhead_rows.append({"arm": arm, "served_family": family, "paired_requests": len(deltas),
                                      "ttft_delta_ms_p25": analyze.pct(deltas, 25), "ttft_delta_ms_p50": analyze.pct(deltas, 50),
                                      "ttft_delta_ms_p75": analyze.pct(deltas, 75), "ttft_delta_ms_mean": statistics.mean(deltas)})

    session_rows = [{k: r.get(k) for k in (
        "run_id", "arm", "router_mode", "reasoning_effort", "question_id", "complexity_tag", "category", "source", "iteration",
        "model_actually_served", "served_model_family", "router_latency_ms", "router_fallback",
        "prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens", "total_tokens", "cost_usd",
        "ttft_ms", "e2e_ms", "status", "response_sha256")} for r in measured]

    write_csv(OUTPUT / "router_arm_summary.csv", arm_rows)
    write_csv(OUTPUT / "router_routing_by_tier.csv", tier_rows)
    write_csv(OUTPUT / "router_routing_by_category.csv", category_rows)
    write_csv(OUTPUT / "router_qira_scenarios.csv", scenario_rows)
    write_csv(OUTPUT / "router_question_hits.csv", hit_rows)
    write_csv(OUTPUT / "router_overhead.csv", overhead_rows)
    write_csv(OUTPUT / "router_single_turn_sessions.csv", session_rows)

    totals = {"all_requests": len(records), "measured_requests": len(measured),
              "all_input_tokens": sum(r["prompt_tokens"] for r in records),
              "all_output_tokens": sum(r["completion_tokens"] for r in records),
              "all_reasoning_tokens": sum(r["reasoning_tokens"] for r in records),
              "all_cost_usd": sum(r["cost_usd"] for r in records),
              "sol_requests_all": sum(1 for r in records if r["served_model_family"] == STRONG)}
    print(json.dumps({"arms": arm_rows, **totals}, ensure_ascii=False, indent=2, default=str))

    write_report(arm_rows, tier_rows, category_rows, scenario_rows, hit_rows, overhead_rows, records, measured, quality, policy_cost, policy_quality, item_of, provenance, totals)
    print(f"VERIFIED: {len(records)} performance rows, {len(measured)} measured single-turn sessions, {len(quality)} quality rows, "
          f"{len(hit_rows)} arm×question hit-rate rows, {len(category_rows)} arm×category rows.")


def pctf(value, digits=0):
    return "-" if value is None else f"{value * 100:.{digits}f}%"


def signed_pct(value):
    return "-" if value is None else f"{value * 100:+.0f}%"


def write_report(arm_rows, tier_rows, category_rows, scenario_rows, hit_rows, overhead_rows, records, measured, quality, policy_cost, policy_quality, item_of, provenance, totals):
    by_arm = {a["arm"]: a for a in arm_rows}
    r_arms = [a for a in arm_rows if a["router_mode"] != "direct"]
    mode_rows = {(a["router_mode"], a["reasoning_effort"]): a for a in r_arms}

    def hits(arm):
        return [h for h in hit_rows if h["arm"] == arm]

    def sol_questions(arm, tier=None):
        return sorted(h["question_id"] for h in hits(arm) if h["sol_hits"] > 0 and (tier is None or h["tier"] == tier))

    consistent_rate = {a["arm"]: sum(1 for h in hits(a["arm"]) if h["consistent"]) / len(hits(a["arm"])) for a in r_arms}
    overhead_txt = {(o["arm"], o["served_family"]): o for o in overhead_rows}
    rl_all = [r["router_latency_ms"] for r in measured if r.get("router_latency_ms") is not None]
    quality_arm = mode_rows[("quality", "not-sent")]
    quality_low = mode_rows[("quality", "low")]
    balanced_arm = mode_rows[("balanced", "not-sent")]
    cost_arm = mode_rows[("cost", "not-sent")]
    sol_none, luna_none = by_arm["gpt-5.6-sol-dz"], by_arm["gpt-5.6-luna-dz"]
    flips_count = sum(not h["consistent"] for h in hit_rows)
    direct_rows = [a for a in arm_rows if a["router_mode"] == "direct"]
    direct_bursts = sum(a["decode_span_lt50ms_requests"] for a in direct_rows)
    direct_n = sum(a["requests"] for a in direct_rows)

    lines = [
        "# Qira 任务 B：Foundry Model Router（GPT-5.6 Sol / GPT-5.6 Luna 两档）路由实测",
        "",
        f"测试日期：2026-09-09/10（UTC）；正式 run：`{RUN}`。目标是回答会上提出的两个问题：**Router 什么时候切到 Sol、什么 case 会切**。",
        "这是受控数据集上的路由行为验证，不是生产并发容量压测，也不是对 Sol/Luna 本身的全面能力评测。",
        "",
        "## 结论先行（只对本轮 47 题、两档子集成立）",
        "",
        f"- **各模式的 Sol 使用比例不同**：`quality` 不发 reasoning_effort 时 {pctf(quality_arm['sol_share'], 1)} 的请求落到 Sol，`low` 时 {pctf(quality_low['sol_share'], 1)}。"
        f"`balanced` 和 `cost` 不发 effort 时 Sol 占比分别为 {pctf(balanced_arm['sol_share'], 1)} / {pctf(cost_arm['sol_share'], 1)}；具体触发题见第 3 节。",
        f"- **人工复杂度标签与实际选择不是同一个概念**：`quality` 模式下 simple/moderate/complex 三档的 Sol 占比为 "
        + " / ".join(pctf(next(t["sol_share"] for t in tier_rows if t["arm"] == "router-sol-luna-quality" and t["tier"] == tier)) for tier in TIERS)
        + "。这只是本轮观测，不能据此推断内部阈值、关键词规则或模型能力需求。",
        f"- **同题稳定性**：全部 {len(hit_rows)} 个 Router arm×题目组合中，{flips_count} 个在 3 次测量间出现模型变化。"
        f"`quality` 模式 3 次测量全部一致的题占 {pctf(consistent_rate['router-sol-luna-quality'])}（`low`：{pctf(consistent_rate['router-sol-luna-quality@low'])}）。"
        "每题给出样本 **Sol 命中率**；3 次观测不代表长期概率或确定性保证。",
        f"- **路由 trace 自报决策耗时**：中位数 {statistics.median(rl_all):.0f} ms、P95 {analyze.pct(rl_all, 95):.0f} ms；"
        f"落到 Luna 的路由请求与直连 Luna 在相同题目上配对比较，TTFT 中位差 {overhead_txt[('router-sol-luna-balanced', CHEAP)]['ttft_delta_ms_p50']:+.0f} ms（balanced）/ "
        f"{overhead_txt[('router-sol-luna-cost', CHEAP)]['ttft_delta_ms_p50']:+.0f} ms（cost）；SKU 与测试时段不同，不能把差值归因于 Router。",
        f"- **成本与质量**：`quality` 模式千次成本 ${quality_arm['cost_usd_per_1000_requests']:.2f}，为全量直连 Sol 的 {(1 + quality_arm['cost_vs_all_sol']) * 100:.0f}%、全量直连 Luna 的 {(1 + quality_arm['cost_vs_all_luna']):.1f} 倍；"
        f"盲评均分 {quality_arm['judge_quality_mean']:.2f}，直连 Sol {sol_none['judge_quality_mean']:.2f}、直连 Luna {luna_none['judge_quality_mean']:.2f}。"
        "在本轮样例上 Luna 单独已经拿到很高的自动评分，因此 Router 的价值不能用本轮质量分差证明，需要用更难、更贴近客户真实流量的题集重测。",
        "",
        "| 想要的效果 | 本轮数据支持的选择 | 说明 |",
        "|---|---|---|",
        "| 让更多请求有机会使用 Sol | 先验证 `quality` 模式 | 不保证只升级人工标为复杂的题；仍需质量验收 |",
        "| 成本优先，主要使用 Luna | 比较 `cost`、`balanced` 与直连 Luna | balanced 存在少量 Sol 命中；不能与 cost 混称全部 Luna |",
        "| 需要确定性、可审计的切换规则 | APIM 策略路由（对照组，未在本轮实测） | 本轮只观察 Model Router 的选择，不证明其内部规则 |",
        "",
        "## 1. 已验证的测试矩阵",
        "",
        "- 3 个 Model Router 部署（model-router 2025-11-18，子集固定为 gpt-5.6-sol 2026-07-09 + gpt-5.6-luna 2026-07-09）：routing mode 分别为 `balanced`、`cost`、`quality`。",
        "- 2 个直连基线：`gpt-5.6-sol-dz`、`gpt-5.6-luna-dz`，作为“全量 Sol”“全量 Luna”两种策略的成本/时延/质量对照。",
        "- 每个部署跑 2 种 reasoning_effort：不发送、`low`；此处不发送不等同于显式 none，也未观测服务内部实际采用的 effort。共 10 个 arm。",
        "- 47 题：30 题受控题（每档 10 题）+ 17 题任务 A 的 Qira 六场景样例；合并后的 simple/moderate/complex 题数分别为 15/18/14。",
        f"- 10 × 47 ×（1 次预热 + 3 次测量）= {totals['all_requests']} 次；{totals['all_requests']}/{totals['all_requests']} completed，0 API 错误、0 截断、0 空答案；{totals['measured_requests']} 次测量。",
        f"- 全部请求走 Chat Completions 流式接口并带 `Foundry-Features: ModelRouterControls=V1Preview`，从响应 `model_selection_details.model_router_details` 读取实际命中模型、路由耗时与尝试链；直连 arm 同样走 Chat Completions 以对齐 API 面。",
        "- 探活被拒的是本轮 Azure OpenAI 资源级 Responses 调用路径；官方另有 Foundry 项目客户端 Responses 路径，不能概括成所有 Responses API 均不支持 Model Router。",
        "- 不挂载搜索或任何工具；同一题所有 arm 的输出上限一致。",
        "",
        "## 2. 环境与边界",
        "",
        f"- Sweden Central Linux VM（{provenance['vm_metadata']['vmSize']}，IMDS 核验 location={provenance['vm_metadata']['location']}）→ 同区 AOAI 资源；与 AOAI 端点的 TCP 连接基线 {sorted({r['network_rtt_ms'] for r in records})[0]} ms。",
        "- **SKU 差异**：3 个 Router 部署为 GlobalStandard；两条直连基线为 DataZoneStandard（Sol GlobalStandard 可用配额不足）。GlobalStandard 可能跨区执行推理，DataZone 限定在 EU 数据区；同区资源不保证同区 GPU，第 5 节的时延差值不是 Router 开销估计或上界。",
        "- **价格口径**：所有成本按 Global 公开价、按**实际命中模型**计费单价计算（Sol $5/$0.50/$30，Luna $0.20/$0.02/$1.20，USD/1M）。DataZone 约高 10% 未计入；Model Router 自身按输入提示收取的路由费在渲染定价页上未找到，**未计入**。",
        "- 两档子集是会上拍板的范围；客户生产环境更可能是 3～4 档，见第 8 节。",
        "- 运行顺序按 arm 串行、并发=1；未随机交错时段，P95 为小样本描述。",
        "- TTFT 从发送请求计到第一个非空文本块；TCP connect 只是网络基线，不能从 TTFT 扣掉后称为纯模型时间。流式块可能包含多个 token，decode/TPOT 是客户端估计而非 GPU 指标。",
        "- 每次请求均包含同一个系统提示词（跨设备助手，直接简洁回答，不提澄清问题）与一条用户问题；没有会话历史，不代表长历史多轮会话的路由行为。",
        "- 本轮切换指不同请求由 Sol 或 Luna 处理，不是观测到生成过程中换模型；Task B 只测不发 effort 与 low，未声称穷尽 Sol 的全部 effort。",
        "- 历史收集器没有保留 finish_reason；完成数依据当时记录的 status、error 与 truncated 字段，无法追溯排除未记录的过滤或缺失终止事件。PR 复核后已加固新运行的终止判断与评分选择；原始数据未改写，原运行代码保存在 source_snapshot 供哈希核验。",
        "",
        "## 3. 什么 case 会切到 Sol",
        "",
        "### 3.1 按复杂度分档（各 Router arm 的 Sol 占比，n = 题数 × 3 次测量）",
        "",
        "| Router arm | simple | moderate | complex | 全部 |",
        "|---|---:|---:|---:|---:|",
    ]
    for arm in [a["arm"] for a in r_arms]:
        cells = [next(t for t in tier_rows if t["arm"] == arm and t["tier"] == tier) for tier in (*TIERS, "ALL")]
        lines.append(f"| {arm} | " + " | ".join(f"{c['sol_requests']}/{c['requests']}（{pctf(c['sol_share'])}）" for c in cells) + " |")
    lines += [
        "",
        "### 3.2 `quality` 模式下被送到 Sol 的题目类别（不发 effort / low）",
        "",
        "| 分档 | 类别 | 题数 | Sol 命中（不发 effort） | Sol 命中（low） |",
        "|---|---|---:|---:|---:|",
    ]
    cat_q = {(c["tier"], c["category"]): c for c in category_rows if c["arm"] == "router-sol-luna-quality"}
    cat_ql = {(c["tier"], c["category"]): c for c in category_rows if c["arm"] == "router-sol-luna-quality@low"}
    for key in sorted(cat_q, key=lambda k: (TIERS.index(k[0]), -cat_q[k]["sol_share"], k[1])):
        a, b = cat_q[key], cat_ql.get(key)
        lines.append(f"| {key[0]} | {key[1]} | {a['questions']} | {a['sol_requests']}/{a['requests']} | " + (f"{b['sol_requests']}/{b['requests']}" if b else "-") + " |")
    lines += [
        "",
        f"`quality`（不发 effort）从未送到 Sol 的题：{', '.join(sorted(set(item_of) - set(sol_questions('router-sol-luna-quality')))) or '（无）'}。",
        f"`quality`（不发 effort）至少一次送到 Sol 的 simple 题：{', '.join(sol_questions('router-sol-luna-quality', 'simple')) or '（无）'}。",
        "",
        "### 3.3 `balanced` / `cost` 模式送到 Sol 的题",
        "",
    ]
    for arm in ("router-sol-luna-balanced", "router-sol-luna-balanced@low", "router-sol-luna-cost", "router-sol-luna-cost@low"):
        qs = sol_questions(arm)
        detail = "、".join(f"{q}（{item_of[q]['tier']}/{item_of[q]['category']}，{next(h['sol_hits'] for h in hits(arm) if h['question_id'] == q)}/3）" for q in qs)
        lines.append(f"- `{arm}`：{len(qs)} 题 — {detail or '无'}")
    lines += [
        "",
        "### 3.4 单独看 Qira 六场景",
        "",
        "下表仅使用 17 道 Qira 题，不与 30 道通用受控题混算；命中数均不含预热。",
        "",
        "| 场景 | 题数 | balanced Sol/请求 | cost Sol/请求 | quality Sol/请求 | quality@low Sol/请求 |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for scenario in sorted({r["scenario"] for r in scenario_rows}):
        cells = [next(r for r in scenario_rows if r["scenario"] == scenario and r["arm"] == arm)
                 for arm in ("router-sol-luna-balanced", "router-sol-luna-cost", "router-sol-luna-quality", "router-sol-luna-quality@low")]
        lines.append(f"| {scenario} | {cells[0]['questions']} | "
                     + " | ".join(f"{r['sol_requests']}/{r['requests']}" for r in cells) + " |")
    lines += [
        "",
        "全部 10 arm×6 场景的命中率、TTFT、标准化成本与自动评分见 [六场景明细](router_qira_scenarios.csv)。"
        "Pay Attention 只测转录文本，Live Interaction 只测文字代理，Creator Zone 只测提示词与编辑意图，不含语音、图像生成或动作执行。",
    ]
    lines += [
        "",
        "## 4. 同题稳定性（每题 3 次测量）",
        "",
        "| Router arm | 题数 | 3 次测量命中同一模型的题 | 出现切换的题 |",
        "|---|---:|---:|---|",
    ]
    for a in r_arms:
        hs = hits(a["arm"])
        flips = [f"{h['question_id']}({h['served_sequence']})" for h in hs if not h["consistent"]]
        lines.append(f"| {a['arm']} | {len(hs)} | {sum(1 for h in hs if h['consistent'])}（{pctf(consistent_rate[a['arm']])}） | {', '.join(flips) or '无'} |")
    lines += [
        "",
        f"本轮有 {flips_count} 个 arm×题目组合在测量之间改变模型。每题 **Sol 命中率**（`router_question_hits.csv`）的分母只有 3，不应表述为已知的生产流量切换概率。",
        "",
        "## 5. 时延与 Router 开销",
        "",
        f"**Decode 边界：直连基线有 {direct_bursts}/{direct_n} 次测量的首末文本块跨度小于 50 ms。**"
        "同一收集器下，直连结果出现集中交付，不能把极高的客户端 decode_tps 当模型生成速度。"
        "本轮保留原值供排查，不据此比较 GPU decode 性能，也未定位集中交付发生在哪一层。",
        "",
        "TTFT/E2E 为客户端观测值（含网络、排队与交付节奏）。客户端 TPOT = 首个文本块到最后一个文本块的跨度 ÷（可见输出 token − 1），只在流式逐块到达时才近似生成速度；“首末<50 ms”列给出整段近乎一次到达的请求数，该值高的行其 TPOT 不可当作模型解码性能。",
        "",
        "| arm | Sol 占比 | 路由决策 P50/P95 ms | TTFT P50/P90/P95 s | E2E P50/P90/P95 s | 客户端 TPOT P50/P90 ms | 首末<50 ms | 输出 Token | 其中推理 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for a in arm_rows:
        rl = "-" if a["router_decision_ms_p50"] is None else f"{a['router_decision_ms_p50']:.0f} / {a['router_decision_ms_p95']:.0f}"
        lines.append(f"| {a['arm']} | {pctf(a['sol_share'])} | {rl} | "
                     f"{a['ttft_p50_ms'] / 1000:.3f} / {a['ttft_p90_ms'] / 1000:.3f} / {a['ttft_p95_ms'] / 1000:.3f} | "
                     f"{a['e2e_p50_ms'] / 1000:.3f} / {a['e2e_p90_ms'] / 1000:.3f} / {a['e2e_p95_ms'] / 1000:.3f} | "
                     f"{a['tpot_p50_ms']:.2f} / {a['tpot_p90_ms']:.2f} | {a['decode_span_lt50ms_requests']}/{a['requests']} | "
                     f"{a['output_tokens_mean']:.0f} | {a['reasoning_tokens_mean']:.0f} |")
    lines += [
        "",
        "配对开销：把路由到模型 F 的请求与直连 F、相同 effort、相同题目的 TTFT 中位数逐题相减（正值 = 经过 Router 更慢）。",
        "",
        "| Router arm | 命中模型 | 配对请求数 | ΔTTFT P25 | ΔTTFT P50 | ΔTTFT P75 |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for o in overhead_rows:
        lines.append(f"| {o['arm']} | {o['served_family']} | {o['paired_requests']} | {o['ttft_delta_ms_p25']:+.0f} ms | {o['ttft_delta_ms_p50']:+.0f} ms | {o['ttft_delta_ms_p75']:+.0f} ms |")
    lines += [
        "",
        "注意：直连基线是 DataZoneStandard、Router 是 GlobalStandard，且两者在不同时段测量；ΔTTFT 混有调度池和负载差异，不能证明 Router 增加、减少或不影响首字时延。",
        "",
        "## 6. 成本与质量 vs 两种直连策略",
        "",
        "| arm | $/千次 | vs 全量 Sol（同 effort） | vs 全量 Luna（同 effort） | $/千次 simple / moderate / complex | 质量/5 | 质量 simple / moderate / complex |",
        "|---|---:|---:|---:|---|---:|---|",
    ]
    for a in arm_rows:
        lines.append(f"| {a['arm']} | {a['cost_usd_per_1000_requests']:.3f} | {signed_pct(a['cost_vs_all_sol'])} | {signed_pct(a['cost_vs_all_luna'])} | "
                     f"{a['cost_per_1000_simple']:.2f} / {a['cost_per_1000_moderate']:.2f} / {a['cost_per_1000_complex']:.2f} | {a['judge_quality_mean']:.2f} | "
                     f"{a['quality_simple']:.2f} / {a['quality_moderate']:.2f} / {a['quality_complex']:.2f} |")
    lines += [
        "",
        "质量：同一个 judge-terra（gpt-5.6-terra）盲评，5 维 1–5 分，每个 arm×题只评第 1 个非预热答案，共 470 份；评分时不知道模型名与路由结果。",
        "自动评分仅作初筛：无法消除同家族偏好与裁判随机性；本轮题目对 Luna 而言大多不难，因此 Sol 与 Luna 的分差很小，不能据此断言“Sol 不值钱”，只能说明**本轮题集不足以区分两档**，下一轮需要更难的题（长上下文、多约束规划、代码推理）。",
        "",
        "## 7. Token 与总体消耗",
        "",
        f"- 正式 {totals['all_requests']} 次（含预热）：输入 {totals['all_input_tokens']:,}；输出 {totals['all_output_tokens']:,}；其中推理 {totals['all_reasoning_tokens']:,}；命中 Sol {totals['sol_requests_all']} 次。",
        f"- 按实际命中模型的 Global 公开价估算模型调用费 **${totals['all_cost_usd']:.4f}**（不含 Router 路由费、judge 调用、探活、VM/磁盘/网络）。",
        "- 每个样例是独立单轮请求；多轮会话需逐轮相加 usage，本轮未测。",
        "",
        "## 8. 扩展到 3～4 档的路径（Roadmap，非本轮结论）",
        "",
        "1. 从 `router_question_hits.csv` 选取稳定命中 Sol 和 Luna 的题作为路由回归样例；另用参考答案与人工评分判断是否真的需要强模型，不能把路由选择本身当质量标签。",
        "2. 经区域与模型支持核验后，在子集中加入中间档或更便宜档，观察 balanced 的分流比例如何变化；本轮两档 balanced 已存在少量 Sol 命中。",
        "3. 对照组：用 APIM 策略（`apim-policy-ptu-routing.xml` 思路）按意图/长度做确定性路由，与 Model Router 在同一题集上比较命中分布、成本和时延，回答“黑盒自动 vs 可控确定”的取舍。",
        "4. 用真实 Qira 多轮会话（带历史）重测：路由器看到的是整段上下文，单轮结论不能直接外推。",
        "",
        "## 9. 可复核文件与保留",
        "",
        f"- [{len(measured)} 次单轮明细](router_single_turn_sessions.csv)、[10 arm 汇总](router_arm_summary.csv)、[分档路由占比](router_routing_by_tier.csv)、[类别路由占比](router_routing_by_category.csv)、[每题命中率](router_question_hits.csv)、[配对开销](router_overhead.csv)。",
        f"- [完整数值 JSONL](router_{RUN}.metrics.jsonl)、[470 份评分](quality_router_{RUN}.jsonl)、[来源与校验](provenance_router_{RUN}.json)、[部署核验](deployment_verification_router.json)。",
        f"- [压缩证据包](evidence_router_{RUN}.json.xz)：保留全部数值字段、路由 trace 与每份输出的 SHA256；不含生成全文。",
        f"- 生成全文 `raw_fulltext/router_{RUN}.jsonl` 由 VM 回收保留，每条 response_text 与数值记录的 response_sha256 逐条一致（`scripts/verify_router_fulltext.py`）。",
        ("- 资源收尾见 [resource_closeout_router.json](resource_closeout_router.json)：VM deallocated（未删除），部署保留供复核。"
         if (OUTPUT / "resource_closeout_router.json").exists()
         else "- VM 资源收尾尚未完成；按要求在 PR 复核后关机并记录 Azure 实际状态，不删除 VM。"),
        "",
    ]
    (OUTPUT / f"Qira-Router-实测结果-{REPORT_DATE}.md").write_text("\n".join(lines), encoding="utf-8", newline="\n")


if __name__ == "__main__":
    main()
