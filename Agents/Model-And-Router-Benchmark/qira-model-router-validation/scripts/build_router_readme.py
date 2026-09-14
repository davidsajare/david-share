"""Generate both Task B READMEs from the verified historical evidence, offline.

The report builder owns the numerical analysis. This reader-facing projection
checks every displayed CSV metric against that builder's archive before writing.
Missing or inconsistent evidence is an error, never a placeholder result.
Direct isolated check: python -I -S scripts/build_router_readme.py --check

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_router_report as report

ROOT = Path(__file__).resolve().parents[1]
RUN = "20260909_223737"
MODES = ("balanced", "cost", "quality")
EFFORTS = ("not-sent", "low")
TIERS = ("simple", "moderate", "complex")
DEPLOYMENTS = (
    "router-sol-luna-balanced",
    "router-sol-luna-cost",
    "router-sol-luna-quality",
    "gpt-5.6-sol-dz",
    "gpt-5.6-luna-dz",
)
ARMS = tuple(d + ("@low" if e == "low" else "") for d in DEPLOYMENTS for e in EFFORTS)
ROUTER_ARMS = ARMS[:6]
DATASET = "datasets/router_taskb.jsonl"
GUIDE = "https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/model-router"
SYSTEM_MESSAGE = (
    "You are a system-level cross-device AI assistant. Answer the user directly "
    "and concisely. Do not ask clarifying questions."
)
LIVE_COMMAND = (
    "python harness.py --mode router --api chat --dataset datasets/router_taskb.jsonl "
    "--deployments " + ",".join(DEPLOYMENTS)
    + " --region swedencentral --client-location swedencentral-linux-vm "
    "--efforts=-,low --iterations 3 --warmup 1"
)
CSV_FILES = {
    "arms": "router_arm_summary.csv",
    "tiers": "router_routing_by_tier.csv",
    "hits": "router_question_hits.csv",
    "scenarios": "router_qira_scenarios.csv",
}
Row = dict[str, Any]


@dataclass
class Evidence:
    dataset: dict[str, Row]
    arms: dict[str, Row]
    tiers: dict[tuple[str, str], Row]
    hits: dict[tuple[str, str], Row]
    scenarios: dict[tuple[str, str], Row]
    measured: list[Row]
    all_requests: int
    all_cost_usd: float


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def keyed(rows: list[Row], fields: tuple[str, ...], expected: set) -> dict:
    result = {
        row[fields[0]] if len(fields) == 1 else tuple(row[f] for f in fields): row
        for row in rows
    }
    require(len(result) == len(rows) and set(result) == expected,
            f"Missing, duplicate, or unexpected CSV cells for {fields}")
    return result


def numeric(row: Row, field: str) -> float:
    value = float(row[field])
    require(math.isfinite(value), f"Non-finite {field}: {row}")
    return value


def agrees(row: Row, field: str, expected: float | None) -> None:
    if expected is None:
        require(row[field] == "", f"{field} should be empty for {row}")
    else:
        require(math.isclose(numeric(row, field), expected, rel_tol=1e-9, abs_tol=1e-9),
                f"CSV {field} differs from archive: {row.get('arm')}, "
                f"{row.get('question_id', row.get('tier', row.get('scenario', 'ALL')))}")


def load_evidence(root: Path = ROOT) -> Evidence:
    output = root / "outputs"
    required = [root / DATASET, output / f"evidence_router_{RUN}.json.xz",
                output / f"provenance_router_{RUN}.json"]
    required.extend(output / name for name in CSV_FILES.values())
    missing = [str(p.relative_to(root)) for p in required if not p.is_file()]
    require(not missing, "Final evidence is not ready; no READMEs written. Missing: " + ", ".join(missing))
    provenance = json.loads(required[2].read_text(encoding="utf-8"))
    require(bool(provenance.get("archive_sha256")), "Provenance has no archive SHA256")
    require(len(report.RUN_SOURCES) == 7 and set(provenance.get("source_sha256", {})) == set(report.RUN_SOURCES),
            "Provenance must identify exactly the seven executed run-source files")
    _, records, quality, _ = report.load_evidence(required[1])
    report.validate_sources(provenance)
    dataset_rows = [json.loads(line) for line in required[0].read_text(encoding="utf-8").splitlines() if line.strip()]
    report.validate(records, quality, dataset_rows)
    require(all(q.get("judge_deployment") == "judge-terra" for q in quality),
            "The README describes only the blind judge-terra evaluation")
    dataset = {row["id"]: row for row in dataset_rows}
    require(Counter(r["source"] for r in dataset_rows) == Counter(router_questions=30, qira_scenarios=17),
            "Expected 30 controlled and 17 Qira prompts")
    scenarios = {r["scenario"] for r in dataset_rows if r["source"] == "qira_scenarios"}
    require(len(scenarios) == 6, "Expected six Qira scenarios")
    rows = {}
    for name, filename in CSV_FILES.items():
        with (output / filename).open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            require(reader.fieldnames is not None and len(reader.fieldnames) == len(set(reader.fieldnames)),
                    f"{filename}: missing or duplicate CSV headers")
            rows[name] = list(reader)
    arms = keyed(rows["arms"], ("arm",), set(ARMS))
    tiers = keyed(rows["tiers"], ("arm", "tier"), {(a, t) for a in ROUTER_ARMS for t in (*TIERS, "ALL")})
    hits = keyed(rows["hits"], ("arm", "question_id"), {(a, q) for a in ROUTER_ARMS for q in dataset})
    scenario_rows = keyed(rows["scenarios"], ("arm", "scenario"), {(a, s) for a in ARMS for s in scenarios})
    measured = [r for r in records if not r["warmup"]]
    quality_map = {(q["arm"], q["question_id"]): q for q in quality}
    pricing = report.analyze.load_pricing(root / "config" / "pricing.json")

    def mean_cost(group: list[Row]) -> float:
        costs: list[float] = []
        for r in group:
            cost = report.analyze.cost_for(pricing, r["served_model_family"], r["prompt_tokens"],
                                          r["cached_tokens"], r["completion_tokens"])
            if cost is None:
                raise ValueError(f"An actual model has no price: {r['served_model_family']}")
            costs.append(cost)
        return statistics.mean(costs)

    def verify_group(row: Row, group: list[Row]) -> None:
        sol = sum(r["served_model_family"] == report.STRONG for r in group)
        agrees(row, "requests", len(group))
        agrees(row, "sol_share", sol / len(group))
        if "sol_requests" in row:
            agrees(row, "sol_requests", sol)
        if "luna_share" in row:
            agrees(row, "luna_share", 1 - sol / len(group))
        agrees(row, "ttft_p50_ms", report.analyze.pct([r["ttft_ms"] for r in group], 50))
        agrees(row, "cost_usd_per_1000_requests", mean_cost(group) * 1000)

    for arm in ARMS:
        group = [r for r in measured if r["arm"] == arm]
        row = arms[arm]
        effort = "low" if arm.endswith("@low") else "not-sent"
        mode = arm.split("@")[0].removeprefix("router-sol-luna-") if arm in ROUTER_ARMS else "direct"
        require(row["deployment"] == arm.split("@")[0] and row["reasoning_effort"] == effort
                and row["router_mode"] == mode, f"CSV arm identity differs: {arm}")
        verify_group(row, group)
        agrees(row, "judge_n", len(dataset))
        agrees(row, "judge_quality_mean",
               statistics.mean(quality_map[(arm, q)]["quality_mean"] for q in dataset))
        agrees(row, "cost_usd_sum", mean_cost(group) * len(group))
        agrees(row, "router_fallbacks", sum(bool(r.get("router_fallback")) for r in group))
        agrees(row, "decode_span_lt50ms_requests",
               sum(r.get("decode_ms") is not None and r["decode_ms"] < 50 for r in group))
        for field, source, percentile in (
            ("ttft_p90_ms", "ttft_ms", 90), ("ttft_p95_ms", "ttft_ms", 95),
            ("e2e_p50_ms", "e2e_ms", 50), ("e2e_p90_ms", "e2e_ms", 90), ("e2e_p95_ms", "e2e_ms", 95),
            ("tpot_p50_ms", "tpot_ms", 50), ("tpot_p90_ms", "tpot_ms", 90), ("decode_tps_p50", "decode_tps", 50),
            ("router_decision_ms_p50", "router_latency_ms", 50),
            ("router_decision_ms_p95", "router_latency_ms", 95),
            ("decode_span_p50_ms", "decode_ms", 50),
        ):
            values = [r[source] for r in group if r.get(source) is not None]
            agrees(row, field, report.analyze.pct(values, percentile) if values else None)
        for scenario in scenarios:
            ids = {q for q, d in dataset.items() if d.get("scenario") == scenario}
            sub = [r for r in group if r["question_id"] in ids]
            sr = scenario_rows[(arm, scenario)]
            verify_group(sr, sub)
            agrees(sr, "questions", len(ids))
            agrees(sr, "judge_quality_mean", statistics.mean(quality_map[(arm, q)]["quality_mean"] for q in ids))
        if arm not in ROUTER_ARMS:
            continue
        for tier in (*TIERS, "ALL"):
            sub = group if tier == "ALL" else [r for r in group if r["complexity_tag"] == tier]
            verify_group(tiers[(arm, tier)], sub)
        for qid, item in dataset.items():
            hr = hits[(arm, qid)]
            sub = sorted((r for r in group if r["question_id"] == qid), key=lambda r: r["iteration"])
            sequence = [r["served_model_family"].removeprefix("gpt-5.6-") for r in sub]
            for field in ("tier", "category", "source"):
                require(hr[field] == item[field], f"Question metadata differs: {arm}/{qid}/{field}")
            agrees(hr, "measured_iterations", len(sub))
            agrees(hr, "sol_hits", sequence.count("sol"))
            agrees(hr, "sol_hit_rate", sequence.count("sol") / len(sub))
            require(hr["served_sequence"] == ">".join(sequence), f"Served sequence differs: {arm}/{qid}")
            require(hr["consistent"] == str(len(set(sequence)) == 1), f"Consistency differs: {arm}/{qid}")
    return Evidence(dataset, arms, tiers, hits, scenario_rows, measured,
                    len(records), mean_cost(records) * len(records))


def table(headers: list[str], rows: list[list[str]]) -> str:
    def line(cells: list[str]) -> str:
        return "| " + " | ".join(str(c).replace("|", r"\|").replace("\n", " ") for c in cells) + " |"
    require(all(len(row) == len(headers) for row in rows), "Ragged documentation table")
    return "\n".join([line(headers), line(["---"] * len(headers)), *(line(row) for row in rows)])


def label(arm: str) -> str:
    deployment = arm.split("@")[0]
    return deployment.removeprefix("router-sol-luna-") if arm in ROUTER_ARMS else deployment


def effort(arm: str) -> str:
    return "low" if arm.endswith("@low") else "-"


def number(row: Row, field: str, digits: int = 1) -> str:
    return f"{numeric(row, field):.{digits}f}"


def fraction(row: Row) -> str:
    return f"{number(row, 'sol_requests', 0)}/{number(row, 'requests', 0)} ({numeric(row, 'sol_share'):.1%})"


def render(evidence: Evidence, language: str, root: Path = ROOT) -> str:
    require(language in ("en", "cn"), f"Unknown language: {language}")
    cn = language == "cn"

    def t(en: str, zh: str) -> str:
        return zh if cn else en

    def heading(anchor: str, en: str, zh: str) -> str:
        return f'<a id="{anchor}"></a>\n## ' + t(en, zh)

    def headers(en: str, zh: str) -> list[str]:
        return t(en, zh).split("|")

    def available(path: str, en: str, zh: str) -> str:
        if (root / path).is_file():
            return f"[{t(en, zh)}]({path})"
        return f"`{path}` — " + t("pending transfer/creation; not yet verified", "待传入或生成，尚未核验")

    arm_table = table(headers(
        "Arm|Effort|N|Sol %|TTFT P50 / P90 / P95 ms|E2E P50 / P90 / P95 ms|Client TPOT P50 / P90 ms|Bursts <50 ms|Output tok (reasoning)|USD / 1,000|Judge / 5",
        "实验组|Effort|N|Sol %|TTFT P50 / P90 / P95 ms|E2E P50 / P90 / P95 ms|客户端 TPOT P50 / P90 ms|首末<50 ms|输出 token（推理）|美元 / 1,000 次|盲评 / 5"), [
            [label(a), effort(a), number(evidence.arms[a], "requests", 0),
             f"{numeric(evidence.arms[a], 'sol_share'):.1%}",
             " / ".join(number(evidence.arms[a], f) for f in ("ttft_p50_ms", "ttft_p90_ms", "ttft_p95_ms")),
             " / ".join(number(evidence.arms[a], f) for f in ("e2e_p50_ms", "e2e_p90_ms", "e2e_p95_ms")),
             number(evidence.arms[a], "tpot_p50_ms", 2) + " / " + number(evidence.arms[a], "tpot_p90_ms", 2),
             number(evidence.arms[a], "decode_span_lt50ms_requests", 0) + "/" + number(evidence.arms[a], "requests", 0),
             number(evidence.arms[a], "output_tokens_mean", 0) + " (" + number(evidence.arms[a], "reasoning_tokens_mean", 0) + ")",
             number(evidence.arms[a], "cost_usd_per_1000_requests", 3),
             number(evidence.arms[a], "judge_quality_mean", 3)] for a in ARMS])
    tier_table = table(headers("Mode|Effort|Simple|Moderate|Complex|All", "模式|Effort|简单|中等|复杂|全部"), [
        [label(a), effort(a), *(fraction(evidence.tiers[(a, tier)]) for tier in (*TIERS, "ALL"))]
        for a in ROUTER_ARMS])
    hit_table = table(headers("Mode|Effort|Question IDs with ≥1 Sol hit", "模式|Effort|至少命中 1 次 Sol 的题号"), [
        [label(a), effort(a), ", ".join(sorted(q for q in evidence.dataset if numeric(evidence.hits[(a, q)], "sol_hits") > 0))
         or t("None", "无")] for a in ROUTER_ARMS])
    scenario_names = sorted({s for _, s in evidence.scenarios})
    scenario_table = table(headers(
        "Qira scenario|Question IDs|N per arm|Balanced Sol (- / low)|Cost Sol (- / low)|Quality Sol (- / low)",
        "Qira 场景|题号|每组 N|Balanced 命中 Sol (- / low)|Cost 命中 Sol (- / low)|Quality 命中 Sol (- / low)"), [
            [s, ", ".join(sorted(q for q, d in evidence.dataset.items() if d.get("scenario") == s)),
             number(evidence.scenarios[(ROUTER_ARMS[0], s)], "requests", 0),
             *(" / ".join(number(evidence.scenarios[(f"router-sol-luna-{m}" + suffix, s)], "sol_requests", 0)
                          for suffix in ("", "@low")) for m in MODES)] for s in scenario_names])
    case_table = table(headers("Mode|Effort|C06 measured decisions|Sol hits", "模式|Effort|C06 测量期选择顺序|Sol 命中次数"), [
        [label(a), effort(a), evidence.hits[(a, "C06")]["served_sequence"],
         number(evidence.hits[(a, "C06")], "sol_hits", 0) + "/3"] for a in ROUTER_ARMS])
    mixed = sum(row["consistent"] == "False" for row in evidence.hits.values())
    trace = [r["router_latency_ms"] for r in evidence.measured if r.get("router_latency_ms") is not None]
    require(bool(trace), "No measured router latency trace")
    trace_median = f"{statistics.median(trace):.0f}"
    trace_p95 = f"{report.analyze.pct(trace, 95):.0f}"
    network_baselines = {r.get("network_rtt_ms") for r in evidence.measured}
    require(len(network_baselines) == 1 and None not in network_baselines,
            "Expected one recorded pre-run TCP-connect baseline")
    network_rtt = numeric({"network_rtt_ms": next(iter(network_baselines))}, "network_rtt_ms")
    require(network_rtt > 0, "Recorded TCP-connect baseline must be positive")
    direct_bursts = sum(int(numeric(evidence.arms[a], "decode_span_lt50ms_requests")) for a in ARMS[6:])
    direct_requests = sum(int(numeric(evidence.arms[a], "requests")) for a in ARMS[6:])
    direct_spans = "; ".join(
        f"{label(a)} ({effort(a)}): {number(evidence.arms[a], 'decode_span_p50_ms')} ms"
        for a in ARMS[6:]
    )
    mode_shares = "; ".join(
        m + " " + " / ".join(f"{numeric(evidence.arms['router-sol-luna-' + m + suffix], 'sol_share'):.1%}"
                             for suffix in ("", "@low")) for m in MODES)
    fulltext = available(f"outputs/raw_fulltext/router_{RUN}.jsonl", "1,880 full answers", "1,880 条完整回答")
    raw_quality = available(f"outputs/raw_fulltext/quality_{RUN}.jsonl", "Original judge output", "原始盲评输出")
    closeout = available("outputs/resource_closeout_router.json", "Resource closeout record", "资源收尾记录")
    parts = [
        t("# Qira Task B: when does Foundry Model Router choose Sol or Luna?",
          "# Qira 任务 B：Foundry Model Router 何时选择 Sol 或 Luna？"),
        "",
        t("A private, reproducible decision brief for a **two-model subset**, not a production routing policy. The platform selects a model per request; you supply deployments and prompts. This run compares three routing modes with two direct baselines.",
          "这份私有仓库中的可复现简报验证 **两模型子集** 的路由行为，不提供生产路由规则。平台逐请求选择模型，使用方提供部署和提示词。本轮对比三种路由模式与两个直连基线。"),
        "",
        "> " + t("Author", "作者") + ": **Xinyu Wei (魏新宇)** · 2026-09-10 · Run `" + RUN + "`",
        "",
        "[English](README.md) | [中文](README-CN.md)",
        "",
        t("[Start here](#start) · [Findings](#findings) · [Reproduce](#reproduce) · [Evidence](#evidence) · [Official guide]",
          "[从这里开始](#start) · [实测结论](#findings) · [复现](#reproduce) · [证据](#evidence) · [官方指南]") + f"({GUIDE})",
        "",
        heading("start", "Start here", "从这里开始"),
        table(headers("Goal|Path|Subscription / side effects", "目标|入口|订阅与副作用"), [
            [t("Read the decision", "了解决策依据"), t("[Results](#results) and [cases](#cases)", "[结果](#results)与[具体题目](#cases)"), t("None", "无需订阅")],
            [t("Rebuild historical evidence", "离线重建历史结果"), t("[Offline commands](#reproduce)", "[离线命令](#reproduce)"), t("Local files only; no model calls", "只处理本地文件，不调用模型")],
            [t("Run a new comparison", "重新运行对比"), t("[Live steps](#live)", "[在线步骤](#live)"), t("Azure subscription, inference RBAC, quota; PAYGO charges", "需要 Azure 订阅、推理权限和配额；按量计费")],
        ]),
        "",
        heading("findings", "Decision brief", "结论先行"),
        t(f"- **Observed Sol request shares (- / low): {mode_shares}.** Both balanced and quality can select Sol; the exact IDs below come from final CSVs.",
          f"- **Sol 请求占比（- / low）：{mode_shares}。** balanced 与 quality 都会选择 Sol；下表题号直接来自最终 CSV。"),
        t("- **Do not equate an author-assigned complexity tier with a router threshold.** Quality mode can select Sol for simple prompts too. Compare cost mode and direct Luna for a budget-oriented starting point; validate quality on your own traffic before choosing.",
          "- **人工复杂度标签不等于路由阈值。** quality 也会为简单题选择 Sol。成本优先时，可先比较 cost 模式与直连 Luna；最终选择仍须通过实际业务数据的质量验收。"),
        t("- **This is descriptive evidence, not a quality winner or a production probability.** The blind judge scores are near the ceiling; small score differences do not establish human-perceived superiority.",
          "- **这是观测结果，不是质量冠军榜，也不是生产选择概率。** 盲评分数接近量表上限，微小分差不能证明人类用户感知的质量优势。"),
        "",
        heading("architecture", "Architecture and test boundary", "架构与测试边界"),
        "```mermaid",
        "flowchart LR",
        '  subgraph SC["Sweden Central: verified client and resource location"]',
        '    VM["Linux VM: Standard_D4s_v5"] --> API["AOAI resource: streaming Chat Completions"]',
        "  end",
        '  API --> MR["model-router: balanced / cost / quality"]',
        '  MR --> GLOBAL["Sol or Luna: GlobalStandard execution"]',
        '  API --> DIRECT["Direct Sol / Luna: DataZoneStandard execution"]',
        '  GLOBAL -.-> BOUNDARY["Model execution is not proven to be in Sweden Central"]',
        "  DIRECT -.-> BOUNDARY",
        '  API --> LOCAL["JSONL answers + traces -> offline CSVs and READMEs"]',
        "```",
        t(f"- IMDS confirms the Sweden Central client VM; the AOAI resource is in the same region. The pre-run median TCP-connect baseline was **{network_rtt:.2f} ms**, not a per-request inference RTT. **This does not prove the GPU/model execution region.** Global and DataZone serving scopes differ.",
          f"- IMDS 确认客户端 VM 位于 Sweden Central，AOAI 资源也在同一区域。测试前 TCP 建连耗时中位数为 **{network_rtt:.2f} ms**，不是逐请求推理往返时延。**这不能证明 GPU 或模型执行位置相同。** Global 与 DataZone 的服务范围不同。"),
        t("- Fixed matrix: 47 synthetic English prompts = 30 controlled (10 each S/M/C) + 17 across six Qira scenarios; combined simple/moderate/complex = 15/18/14. Five deployments × two efforts × (1 warmup + 3 measured) = **1,880 performance rows; 1,410 measured; 470 blind Terra scores**.",
          "- 固定矩阵：47 条合成英文提示词，其中 30 条受控题（S/M/C 各 10 条）、17 条覆盖 Qira 六场景；合并后简单/中等/复杂为 15/18/14。五个部署 × 两种 effort ×（1 次预热 + 3 次测量）= **1,880 条性能记录、1,410 条测量记录、470 条 Terra 盲评分数**。"),
        t("- Each case is single-turn: **a common system message followed by one user message, with no conversation history**. It is not a system-prompt-free request. Inputs are text-only, without web search or tools. Live Interaction and Creator Zone are text proxies, not voice/image end-to-end tests. “Switch” means selection for a request, not mid-generation handoff.",
          "- 每个案例均为单轮请求：**先发送统一的系统消息，再发送一条用户消息，不携带历史对话**。单轮不等于没有系统提示词。输入均为纯文本，不启用网络搜索或工具。Live Interaction 与 Creator Zone 只验证文本代理任务，不是语音或图像端到端测试。“切换”指逐请求选择，不是生成中途交接。"),
        t("- `-` means **reasoning_effort NOT SENT**, not explicit `none`; `low` is sent explicitly on all five deployments. These are two sampled settings, **not exhaustive effort coverage**. Equal output caps per question do not imply equal output lengths.",
          "- `-` 表示 **不发送 reasoning_effort**，不等于显式 `none`；五个部署均另测显式 `low`。本轮只覆盖这两种设置，**不是 effort 全覆盖**。同题输出上限相同，不代表实际输出长度相同。"),
        "",
        heading("results", "Ten-arm results", "十组实测结果"),
        t("Measured requests only; each arm has 141 requests and 47 judged answers. Short mode labels map to the deployment names in the live command.",
          "仅统计测量期请求；每组 141 次请求、47 条被评分回答。模式简称对应在线命令中的部署名称。"),
        arm_table,
        t("TTFT is client elapsed time to the first non-empty text delta; E2E is elapsed time to stream completion. Client TPOT is the first-to-last text-delta span divided by (visible output tokens − 1); it approximates generation speed only when deltas arrive token by token, so read it next to the **Bursts <50 ms** column: rows where most answers arrived in one burst carry TPOT values that describe delivery, not decoding. All are observed client times, including network/queueing and delivery behavior, not pure model time or isolated prefill compute. P50/P90/P95 use the report builder's nearest-rank percentile method.",
          "TTFT 是客户端到首个非空文本增量的耗时；E2E 是到流式响应结束的总耗时。客户端 TPOT = 首个到最后一个文本增量的跨度 ÷（可见输出 token − 1），只有在逐 token 到达时才近似生成速度，因此要对照 **首末<50 ms** 列一起读：多数回答一次性到达的行，其 TPOT 描述的是交付节奏而不是解码。以上都是客户端观测值，包含网络、排队及响应交付行为，不是纯模型耗时，也不能单独代表 prefill 计算。P50/P90/P95 沿用报告生成器的最近秩分位数算法。"),
        t(f"**Observed burst delivery:** {direct_bursts}/{direct_requests} measured direct requests had a first-to-last text-delta span (`decode_ms`) **<50 ms**. Direct-arm median spans: {direct_spans}. Router and direct paths used the same collector and streaming Chat Completions API. **The responsible layer/cause was not diagnosed.** Apparent `decode_tps` is not a model/GPU generation rate and must not be used to rank direct baselines as faster decoders.",
          f"**观测到成批交付：**{direct_bursts}/{direct_requests} 次直连测量请求的首个至最后一个文本增量间隔（`decode_ms`）**<50 ms**。直连各组的间隔中位数：{direct_spans}。路由与直连使用同一采集器及流式 Chat Completions API。**造成该现象的层级与原因尚未诊断。** 表观 `decode_tps` 不是模型或 GPU 的生成速率，不能据此将直连基线排为解码更快的模型。"),
        t("Costs use the **actual served model**, normalized to Global list rates in USD per 1M input/cached/output tokens: **Sol 5 / 0.5 / 30; Luna 0.2 / 0.02 / 1.2**. Formula: `((input-cached)*input_rate + cached*cached_rate + output*output_rate)/1e6`; output includes reasoning tokens. USD/1,000 = mean request cost × 1,000.",
          "成本按**实际服务模型**计算，统一使用 Global 标价，单位为美元/每 1M 输入、缓存输入、输出 token：**Sol 5 / 0.5 / 30；Luna 0.2 / 0.02 / 1.2**。公式：`((input-cached)*input_rate + cached*cached_rate + output*output_rate)/1e6`；输出含推理 token。每 1,000 次成本 = 单次平均成本 × 1,000。"),
        t(f"The derived Global-normalized model-token total across **all {evidence.all_requests:,} requests, including warmups, is ${evidence.all_cost_usd:.7f}**; it uses the same exclusions as the measured-only table.",
          f"按同一 Global 标价归一化计算，**全部 {evidence.all_requests:,} 次请求（含预热）的模型 token 成本合计为 ${evidence.all_cost_usd:.7f}**；费用排除项与仅统计测量期的表格相同。"),
        t("This is **not an Azure bill**: it excludes DataZone premiums, any router fee (not verified), judge, VM and probes; the table excludes warmups. The judge scores one earliest valid measured answer per arm/question, blinded to candidate identity, across five 1–5 dimensions. It may have ceiling bias and is not a human quality verdict.",
          "这**不是 Azure 实际账单**：未计入 DataZone 溢价、路由费用（尚未核验）、盲评、VM 与探测费用；表格不含预热。盲评对每组每题最早的有效测量回答评分，隐藏候选模型身份，包含五个 1–5 分维度。评分可能存在天花板偏差，不能代替人工质量结论。"),
        "",
        heading("cases", "Which requests choose Sol?", "哪些请求选择 Sol？"),
        t("Cells are Sol hits / measured requests (share). Each question contributes N=3, not a production probability.",
          "单元格为 Sol 命中次数/测量请求数（占比）。每题 N=3，不是生产选择概率。"),
        tier_table,
        "",
        hit_table,
        t(f"Across {len(evidence.hits)} router arm/question cells, **{mixed}** changed served model between the three measured repetitions. The [per-question CSV](outputs/router_question_hits.csv) retains every sequence and warmup decision; repeated agreement does not guarantee deterministic future routing.",
          f"在 {len(evidence.hits)} 个路由实验组/题目组合中，**{mixed}** 个在三次测量间出现服务模型变化。[逐题 CSV](outputs/router_question_hits.csv)保留全部选择顺序及预热选择；重复结果一致也不能保证未来路由确定不变。"),
        "",
        t("### Qira-only scenario coverage", "### 仅 Qira 子集的场景覆盖"),
        t(f"This is the **17-prompt Qira subset**, not all 47 cases: the other 30 are controlled prompts. Each mode cell lists Sol counts for `- / low`; divide either count by N for its share. The [Qira-only scenario CSV](outputs/router_qira_scenarios.csv) contains **{len(evidence.scenarios)} rows = 10 arms × 6 scenarios**, including question/request counts, Sol hits/share, TTFT P50, normalized cost and judge quality.",
          f"这里仅统计 **17 条 Qira 提示词子集**，不是全部 47 个案例；其余 30 条是受控题。每个模式单元格依次列出 `- / low` 的 Sol 命中次数，分别除以 N 即为占比。[Qira 专属场景 CSV](outputs/router_qira_scenarios.csv)包含 **{len(evidence.scenarios)} 行 = 10 组 × 6 场景**，提供题数、请求数、Sol 命中次数与占比、TTFT P50、归一化成本和盲评质量。"),
        scenario_table,
        "",
        heading("example", "One exact input and its observed path: C06", "一条完整输入及其观测路径：C06"),
        t("Every Chat request first sends this exact shared `SYSTEM_MSG` as `role: system`, including all router and direct arms; see [the harness](harness.py):",
          "每个 Chat 请求都先以 `role: system` 发送以下统一的 `SYSTEM_MSG`，所有路由组与直连组均相同；见 [harness](harness.py)："),
        "```text",
        SYSTEM_MESSAGE,
        "```",
        t("The second message is `role: user`. For C06, the synthetic `complex / code_reasoning` prompt is copied verbatim from the [owned dataset](datasets/router_taskb.jsonl), not translated or shortened:",
          "第二条消息为 `role: user`。C06 属于 `complex / code_reasoning` 合成题，以下内容逐字取自[仓库内数据集](datasets/router_taskb.jsonl)，未翻译或缩短："),
        "```text",
        evidence.dataset["C06"]["text"],
        "```",
        case_table,
        t("Run chain: shared system message + dataset C06 user message → streaming request with the selected deployment/effort → response `model` plus `model_selection_details.model_router_details` → local answer and trace JSONL → SHA256-linked numerical archive → CSVs → this table. The full sequence is request-level, not a reconstructed internal decision rule.",
          "运行链：统一系统消息 + 数据集 C06 用户消息 → 使用指定部署和 effort 发起流式请求 → 响应 `model` 与 `model_selection_details.model_router_details` → 本地回答及 trace JSONL → 以 SHA256 关联的数值归档 → CSV → 本表。这里记录的是请求级路径，不是对内部决策规则的逆向推断。"),
        "",
        heading("reproduce", "Reproduce: offline first, then optional paid live run", "复现：先离线，按需在线付费运行"),
        t("### 1. Get the code and prepare Python", "### 1. 获取代码并准备 Python"),
        t("The offline path needs **Python 3.10+ and the standard library only**; no pip packages, Azure credentials or GPU. Commands below are Linux Bash, not PowerShell; the reference VM used Python 3.12.3.",
          "离线路径只需 **Python 3.10+ 及标准库**，无需 pip 包、Azure 凭据或 GPU。以下命令为 Linux Bash，不是 PowerShell；参考 VM 使用 Python 3.12.3。"),
        "```bash",
        "git clone https://github.com/david-xinyuwei/david-share.git",
        "cd david-share/Agents/Model-And-Router-Benchmark/qira-model-router-validation",
        "python3 -m venv --without-pip .venv-offline",
        "source .venv-offline/bin/activate",
        "```",
        t("### 2. Rebuild and verify the historical run (no Azure/model calls)", "### 2. 重建并核验历史运行（不调用 Azure 或模型）"),
        "```bash",
        "python scripts/build_router_report.py",
        "python scripts/verify_router_fulltext.py",
        "python scripts/build_router_readme.py",
        "python scripts/validate_router_readme.py",
        "python -m unittest discover -s tests",
        "```",
        t(f"Done when the report verifies 1,880/1,410/470 rows, the full-text verifier matches all 1,880 answers to SHA256 and numerical fields, and documentation/tests pass. Missing retained files are a real blocker, not a reason to create mock answers. The report/full-text builders target **only `{RUN}`**; they do not auto-select a new run.",
          f"验收条件：报告确认 1,880/1,410/470 条记录，全文校验器确认全部 1,880 条回答的 SHA256 与数值字段一致，文档及测试通过。缺少留存文件是真实阻塞，不能用模拟回答补齐。报告与全文校验器**只针对 `{RUN}`**，不会自动选择新运行。"),
        "",
        '<a id="live"></a>',
        t("### 3. Provision and run a new experiment (PAYGO)", "### 3. 配置并执行新实验（PAYGO）"),
        t("**Live only:** install [the recorded SDK requirements](requirements.txt), `openai==3.10.0` and `azure-identity==1.25.3`, in a separate environment; Azure CLI is also needed for CLI login. These versions were recorded on the benchmark VM. Local Windows SDK restoration was blocked by a stale corporate package mirror and TLS handshake failures against official wheel downloads: **Windows live installation is NOT VERIFIED**. TLS verification was not bypassed.",
          "**仅在线运行需要：**在独立环境中安装[记录的 SDK 依赖](requirements.txt)：`openai==3.10.0` 和 `azure-identity==1.25.3`；使用 CLI 登录时还需 Azure CLI。这些版本已在基准 VM 上留存。此次本地 Windows SDK 恢复受过期企业包镜像及官方下载 wheel 的 TLS 握手失败阻塞，**Windows 在线安装尚未核验（NOT VERIFIED）**。未绕过 TLS 校验。"),
        "```bash",
        "deactivate",
        "python3 -m venv .venv-live",
        "source .venv-live/bin/activate",
        "python -m pip install -r requirements.txt",
        "```",
        t(f"1. In the [official deployment guide]({GUIDE}), create a Sweden Central resource and a same-region Linux VM (`Standard_D4s_v5` for the reference run). Verify both resource location and VM IMDS; a CLI region label alone is not proof. You need deployment-management permission and sufficient quota; no turnkey router provisioning helper is supplied.",
          f"1. 按[官方部署指南]({GUIDE})创建 Sweden Central 资源及同区域 Linux VM（参考运行使用 `Standard_D4s_v5`）。核验资源位置与 VM IMDS；单凭 CLI 区域标签不能证明同区域。需要部署管理权限和足够配额；本仓库未提供一键创建路由部署的工具。"),
        t("2. Create three `model-router` **2025-11-18**, `GlobalStandard`, capacity **300** deployments named `router-sol-luna-balanced`, `router-sol-luna-cost`, `router-sol-luna-quality`. In Custom settings set the corresponding routing mode; enable **Route to a subset of models**, selecting only `gpt-5.6-sol` and `gpt-5.6-luna`, both **2026-07-09**. Do not leave the default full model set. Wait **five minutes** after mode changes, then verify the saved settings.",
          "2. 创建三个 `model-router` **2025-11-18** 部署，均为 `GlobalStandard`、capacity **300**，名称分别为 `router-sol-luna-balanced`、`router-sol-luna-cost`、`router-sol-luna-quality`。在 Custom settings 中设置对应模式，启用 **Route to a subset of models**，只选择 `gpt-5.6-sol` 与 `gpt-5.6-luna`，版本均为 **2026-07-09**。不要保留默认全模型集合。修改模式后等待**五分钟**，再核验保存的配置。"),
        t("3. Separately deploy `gpt-5.6-sol-dz` and `gpt-5.6-luna-dz` as the corresponding **2026-07-09** models, `DataZoneStandard`, capacity **300**. These are comparison baselines, not router prerequisites. Sol Global quota was unavailable in this run; capacity is a deployment setting, not measured concurrency. For optional live scoring, create a separate Terra deployment named `judge-terra`.",
          "3. 单独创建 `gpt-5.6-sol-dz` 与 `gpt-5.6-luna-dz`，分别部署对应 **2026-07-09** 模型，均为 `DataZoneStandard`、capacity **300**。它们是对比基线，不是路由所需前置部署。本轮 Sol Global 配额不可用；capacity 是部署配置，不是实测并发数。如需在线盲评，另建 Terra 部署 `judge-terra`。"),
        t("4. Give the calling Entra identity the **Cognitive Services OpenAI User** inference role on the resource (management permission alone is insufficient). [The client](harness.py) uses `DefaultAzureCredential` when no key is set: Azure CLI login or a configured VM managed identity can supply credentials. No API key is required; do not store tokens or secrets in the repository.",
          "4. 为调用方 Entra 身份授予资源级 **Cognitive Services OpenAI User** 推理角色（只有管理权限不够）。未设置密钥时，[客户端](harness.py)通过 `DefaultAzureCredential` 使用 Azure CLI 登录身份或已配置的 VM 托管身份。无需 API key，不要将令牌或密钥写入仓库。"),
        "```bash",
        "az login",
        "unset AZURE_OPENAI_API_KEY",
        'export AZURE_OPENAI_ENDPOINT="https://<RESOURCE_NAME>.openai.azure.com/"',
        'export AZURE_OPENAI_API_VERSION="2025-04-01-preview"',
        "```",
        t("5. Preflight checks the 10-arm matrix without inference calls. With an endpoint set it also opens TCP connections for RTT, so it is not strictly network-free; it does not prove deployment availability or authorization. For managed identity, skip interactive `az login` and configure identity/RBAC before this step.",
          "5. Preflight 在不调用推理接口的情况下检查 10 组矩阵；设置 endpoint 时仍会建立 TCP 连接测 RTT，因此并非完全无网络。它不能证明部署可用或授权成功。使用托管身份时可跳过交互式 `az login`，但须预先配置身份与 RBAC。"),
        "```bash",
        LIVE_COMMAND + " --preflight",
        "```",
        t("6. The following command sends **1,880 paid requests**. All use streaming Chat Completions with `Foundry-Features: ModelRouterControls=V1Preview`; [the harness](harness.py) adds the header and records the actual model. Preserve its printed output path and run ID.",
          "6. 以下命令发送 **1,880 次付费请求**。全部使用流式 Chat Completions，并带 `Foundry-Features: ModelRouterControls=V1Preview`；[harness](harness.py)负责添加请求头并记录实际模型。保留其打印的输出路径和 run ID。"),
        "```bash",
        LIVE_COMMAND,
        "```",
        t("7. Set `RUN_ID` to the **new** ID printed above. Scoring is a separate, paid, post-run operation; the default `--max-chars 0` preserves the full prompt and answer (no clipping). The analysis command is offline.",
          "7. 将 `RUN_ID` 替换为上一步打印的**新** ID。盲评是性能测试结束后的独立付费操作；默认 `--max-chars 0` 保留完整提示词和回答，不裁剪。分析命令离线运行。"),
        "```bash",
        'RUN_ID="YYYYMMDD_HHMMSS"',
        'python judge.py "outputs/router_${RUN_ID}.jsonl" --dataset datasets/router_taskb.jsonl --judge-deployment judge-terra --max-chars 0',
        'python analyze.py "outputs/router_${RUN_ID}.jsonl" --mode router --quality "outputs/quality_${RUN_ID}.jsonl"',
        "```",
        t("Done when the new JSONL contains all 1,880 completed, nontruncated answers, 1,410 measured rows, actual model identities and 470 valid judge scores. Keep the raw answers and hashes; use the generic analysis above for new runs, not the fixed historical builder. Re-running creates another timestamped file and incurs charges; there is no resume/checkpoint contract. Stop/deallocate resources after preserving evidence and record closeout.",
          "验收条件：新 JSONL 包含全部 1,880 条完成且未截断的回答、1,410 条测量记录、实际模型身份和 470 条有效盲评分数。保留原始回答及哈希；新运行使用上述通用分析命令，不使用固定历史报告生成器。重跑会创建新的时间戳文件并再次计费，不提供断点续跑契约。保存证据后停止或释放资源，并记录收尾状态。"),
        "",
        heading("limits", "Interpretation limits and untested next steps", "解释边界与尚未测试的后续方案"),
        t("- Post-PR fixes reject filtered/unterminated Chat streams and exclude incomplete answers from scoring. A second review pass then closed three more gaps in the runnable code: the router analyzer now applies the same completed/non-truncated/usage-present filter as the direct analyzer and refuses mixed environments instead of printing tables; a stream that ends without a usage chunk is recorded as `missing_usage` (not a zero-cost success) and rejected by the report validator; the validator also requires the routing trace's single successful attempt to name the served model; and the SDK's two silent retries are disabled (`max_retries=0`) so every 429/5xx becomes a recorded error rather than hidden TTFT. The historical run did not retain `finish_reason`: its completion counts rely on the recorded status/error/truncation flags, so unrecorded terminal events cannot be ruled out retrospectively. Historical answers are unchanged; [executed source snapshots](outputs/source_snapshot/manifest.json) preserve the original hashes separately from the hardened runnable code.",
          "- PR 复核后，新收集器会拒绝被过滤或缺失终止事件的 Chat 响应，评分也会排除未完成的回答。第二轮复核又补上了可运行代码中的三个缺口：Router 分析器现在与直连分析器使用同一套“已完成 / 未截断 / 有 usage”过滤，遇到混合环境直接拒绝而不是照常输出表格；没有 usage 块的流记为 `missing_usage`（不再算零成本成功），报告校验器同样拒绝；校验器还要求路由 trace 中唯一成功的 attempt 与实际服务模型一致；并关闭了 SDK 的两次静默重试（`max_retries=0`），每次 429/5xx 都会成为可见错误而不是被藏进 TTFT。历史运行未保留 `finish_reason`，完成数依据已记录的状态、错误与截断标志，无法追溯排除未记录的终止事件。历史回答未改写；[原运行代码快照](outputs/source_snapshot/manifest.json)保留原始哈希，与加固后的可运行代码分开。"),
        t(f"- Router traces self-report decision time of **{trace_median} ms median / {trace_p95} ms P95** across measured router requests at `model_selection_details.model_router_details`. This is a service-reported component, not an independently isolated network-inclusive cost.",
          f"- 测量期路由请求的 `model_selection_details.model_router_details` trace 自报决策耗时为**中位数 {trace_median} ms / P95 {trace_p95} ms**。这是服务自报的组成部分，不是独立隔离测得、包含网络的总开销。"),
        t("- Global router vs DataZone direct SKU and fixed serial arm ordering confound TTFT differences. The legacy-named [router_overhead.csv](outputs/router_overhead.csv) contains paired TTFT **differences**, not router overhead estimates or upper bounds. Time-of-run effects, stream buffering and serving scope remain uncontrolled.",
          "- 路由 Global SKU 与直连 DataZone SKU 不同，且实验组按固定顺序串行运行，TTFT 差值存在混杂。沿用历史文件名的 [router_overhead.csv](outputs/router_overhead.csv)保存的是配对 TTFT **差值**，不是路由开销估计或上界。运行时段、流缓冲和服务范围等因素尚未控制。"),
        t(f"- The tested **AOAI resource-level Responses path** rejected the router. The [official Foundry project-level Responses path]({GUIDE}#use-model-router) exists and was not tested here; this is not a claim that Responses is universally unsupported.",
          f"- 本轮被拒绝的是 **AOAI 资源级 Responses 路径**。官方另有 [Foundry 项目级 Responses 路径]({GUIDE}#use-model-router)，本轮未测；不能概括为 Responses 一律不支持路由。"),
        t("- **NOT TESTED:** a 3–4-tier candidate set, APIM policy routing, deterministic escalation rules, multi-turn state transfer, concurrency/load limits, or production SLAs. A future APIM comparator should log policy decisions and test latency/quality/cost against the same owned prompts; it is not a result of this experiment.",
          "- **尚未测试（NOT TESTED）：**3–4 档候选模型、APIM 策略路由、确定性升级规则、多轮状态传递、并发与负载上限、生产 SLA。未来的 APIM 对照组应记录策略决策，并在同一自有题集上比较时延、质量与成本；这不是本轮实验结果。"),
        "",
        heading("evidence", "Evidence map and lifecycle", "证据索引与资源生命周期"),
        table(headers("Evidence|What it establishes", "证据|可核验内容"), [
            [f"[Archive](outputs/evidence_router_{RUN}.json.xz) · [provenance](outputs/provenance_router_{RUN}.json)",
             t("Numerical archive SHA256, exactly 7 executed-source hashes, IMDS and package versions; original vs sanitized hashes are distinguished", "数值归档 SHA256、确切的 7 个运行源码哈希、IMDS 与包版本；区分原始文件和脱敏文件哈希")],
            ["[Deployment verification](outputs/deployment_verification_router.json)",
             t("Captured region, versions, modes, subset and SKUs; not current live status", "留存的区域、版本、模式、子集与 SKU，不代表实时状态")],
            ["[Arm summary](outputs/router_arm_summary.csv) · [tier](outputs/router_routing_by_tier.csv) · [category](outputs/router_routing_by_category.csv)",
             t("Measured aggregates; [individual requests](outputs/router_single_turn_sessions.csv) retain actual model and response SHA256", "测量期汇总；[逐请求记录](outputs/router_single_turn_sessions.csv)保留实际模型及回答 SHA256")],
            [f"[Metrics JSONL](outputs/router_{RUN}.metrics.jsonl) · [blind scores](outputs/quality_router_{RUN}.jsonl)",
             t("1,880 numerical rows and 470 offline-inspectable scores from post-run Terra calls", "1,880 条数值记录，以及由测试后 Terra 调用生成、可离线检查的 470 条评分")],
            [fulltext + " · " + raw_quality,
             t("[Full-text verifier](scripts/verify_router_fulltext.py) checks answer hashes and numerical fields; no fabricated replacements", "[全文校验器](scripts/verify_router_fulltext.py)比对回答哈希与数值字段，不使用伪造替代数据")],
            ["[Detailed Chinese report](outputs/Qira-Router-实测结果-20260910.md)",
             t("Deep per-category/scenario, cost, trace and quality analysis from the same report builder", "同一报告生成器输出的类别、场景、成本、trace 与质量详细分析")],
            [closeout, t("Check this record before asserting resources are stopped; no shutdown or PR-review completion is claimed here", "宣称资源停止前须核验此记录；本文不宣称已关机或已完成 PR 审查")],
        ]),
        t("Both READMEs are generated by [one renderer](scripts/build_router_readme.py), with numbers cross-checked against the archive and CSVs. Edit that renderer, regenerate both files, then run the [documentation validator](scripts/validate_router_readme.py). Presence of a full-text/closeout link alone is not proof of a successful hash check or shutdown.",
          "两份 README 由[同一生成器](scripts/build_router_readme.py)生成，数字与归档及 CSV 交叉核验。修改生成器后同时重建两份文件，再运行[文档校验器](scripts/validate_router_readme.py)。全文或收尾链接存在，本身不代表哈希校验成功或资源已经关闭。"),
        "",
        heading("sources", "Related work and official references", "相关工作与官方参考"),
        t("- [Scenario model benchmark](../qira-scenario-model-benchmark/README.md): companion direct-model work, not evidence that this study covers every effort.",
          "- [场景模型基准](../qira-scenario-model-benchmark/README-CN.md)：配套直连模型工作，不代表本研究覆盖所有 effort。"),
        t("- [Source methodology](../README.md): benchmark lineage; this folder contains its own input and executable path.",
          "- [方法来源](../README-CN.md)：基准方法沿革；本目录自带输入与可执行路径。"),
        f"- [Model Router deployment, modes, subset and API guide]({GUIDE}) · [Deployment types and serving scope](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/deployment-types) · [Entra authentication](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/how-to/configure-entra-id).",
    ]
    return "\n".join(parts) + "\n"


def build_documents(root: Path = ROOT) -> dict[str, str]:
    evidence = load_evidence(root)
    return {name: render(evidence, lang, root) for name, lang in (("README.md", "en"), ("README-CN.md", "cn"))}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if either README is missing or stale; do not write")
    args = parser.parse_args()
    documents = build_documents()
    if args.check:
        stale = [name for name, text in documents.items()
                 if not (ROOT / name).is_file() or (ROOT / name).read_text(encoding="utf-8") != text]
        require(not stale, "Stale generated documentation: " + ", ".join(stale))
        print("VERIFIED: both READMEs match the final evidence and shared renderer.")
        return
    for name, text in documents.items():
        (ROOT / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"WROTE: {name} ({len(text.splitlines())} lines)")


if __name__ == "__main__":
    main()
