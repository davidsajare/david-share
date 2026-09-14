"""Validate the committed evidence archive and regenerate every CSV and report.

Runs offline in about two seconds; refuses to build if any integrity gate fails.

Usage:
    python scripts/build_results_report.py

Author: Xinyu Wei (魏新宇)
"""

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
import analyze

RUN = "20260909_120534"
OUTPUT = ROOT / "outputs"
SCENARIOS = (
    "NextMove", "WriteForMe", "CatchMeUp", "PayAttention",
    "LiveInteraction", "CreatorZone",
)
# SHA256 of the public evidence archive. The public builder intentionally
# accepts only this digest: accepting a known pre-publication archive would
# allow the withdrawn transcript text to be reintroduced while still passing.
ARCHIVE_SHA256 = {
    "e3ed47adfa25c0da1234496095436a880fa0d16a168ed6aecf0efa0eb64a94a8":
        "public (transcript prompts withheld, numbers unchanged)",
}
ARCHIVE_SOURCE_SHA256 = {
    "harness.py": "466d6dd74f15f1e95c01d7a073d750b41209eaf46825777b2c49e18180863529",
    "datasets/qira_scenarios.jsonl":
        "30d4958301ba6321f455211a6be463c8bdfb30c78416493efdd4136721656cd6",
}
PUBLIC_BUILD_INPUT_SHA256 = {
    "harness.py": "50fdc233bd841cad54ae8b7f9ee7d7fb4b866e2da3e141e213c3ab9ef68ecce3",
    "analyze.py": "48411387d4e1b10582d992437f8b3b1a9526ac2fff039aa2174210bd4e47b32c",
    "config/models.json":
        "380259c3eb57e8d2b1c86f2c2c0a5134d3acb920c86739fb92de24c97bb7749f",
    "config/pricing.json":
        "9c5ddf0050b4ae5e6cc1fcb3a07313f711c67229d2d922382cc55e6d7ffeefed",
    "datasets/qira_scenarios.jsonl":
        "f6087d40482d7749c4fe8398dbbb583447fe78bc77d1cf87b23c039f6c066c21",
}


def validate_sources(provenance):
    if provenance.get("source_sha256") != ARCHIVE_SOURCE_SHA256:
        raise ValueError("Archive source hashes differ from the pinned executed run.")
    for name, expected in PUBLIC_BUILD_INPUT_SHA256.items():
        actual = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"Public build input differs from its pinned hash: {name}")


def mean(rows, field):
    return statistics.mean(r[field] for r in rows)


def write_csv(path, rows):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main():
    archive = OUTPUT / f"evidence_{RUN}.json.xz"
    payload = archive.read_bytes()
    archive_variant = ARCHIVE_SHA256.get(hashlib.sha256(payload).hexdigest())
    if archive_variant is None:
        raise ValueError("Evidence archive does not match any verified checksum.")
    print(f"evidence archive: {archive_variant}")
    evidence = json.loads(lzma.decompress(payload))
    if evidence["provenance"]["vm_metadata"]["location"].casefold() != "swedencentral":
        raise ValueError("IMDS does not confirm the benchmark VM region.")
    validate_sources(evidence["provenance"])
    records = [
        dict(zip(evidence["performance_columns"], values, strict=True))
        for values in evidence["performance_values"]
    ]
    quality = evidence["quality"]
    registry = analyze.load_registry(ROOT / "config" / "models.json")
    targets = ("gpt-5.6-luna", "gpt-5-mini", "gpt-4o-mini-bench")
    expected_arms = {
        f"{dep}@{effort}" if effort else dep
        for dep in targets
        for effort in (registry[dep]["supported_efforts"] or [None])
    }
    dataset = [json.loads(line) for line in (ROOT / "datasets" / "qira_scenarios.jsonl").read_text(encoding="utf-8").splitlines()]
    expected_ids = {item["id"] for item in dataset}
    expected_keys = {(arm, qid, iteration) for arm in expected_arms for qid in expected_ids for iteration in range(1, 5)}
    actual_keys = [(r["arm"], r["question_id"], r["iteration"]) for r in records]
    if len(records) != 748 or len(set(actual_keys)) != 748 or set(actual_keys) != expected_keys:
        raise ValueError("Missing, duplicated, or unexpected matrix cells.")
    conditions = {(r["region"], r["endpoint_host"], r["deployment_type"], r["client_location"]) for r in records}
    if len(conditions) != 1:
        raise ValueError("Mixed test environments.")
    for r in records:
        if r["status"] != "completed" or r["error"] or r["truncated"] or r["tools_enabled"] is not False:
            raise ValueError(f"Invalid measurement: {r['arm']}/{r['question_id']}")
        if r["warmup"] != (r["iteration"] == 1):
            raise ValueError("Incorrect warmup flag.")
        if not 0 <= r["cached_tokens"] <= r["prompt_tokens"] or not 0 <= r["reasoning_tokens"] <= r["completion_tokens"]:
            raise ValueError("Inconsistent token accounting.")
        if r["response_chars"] <= 0 or r["ttft_ms"] is None or r["ttft_ms"] > r["e2e_ms"]:
            raise ValueError("Empty or inconsistent timing record.")
    for qid in expected_ids:
        if len({r["max_output_tokens"] for r in records if r["question_id"] == qid}) != 1:
            raise ValueError("Output cap differs across arms for the same question.")
    qmap = {(q["arm"], q["question_id"]): q for q in quality}
    if len(quality) != 187 or len(qmap) != 187 or set(qmap) != {(a, q) for a in expected_arms for q in expected_ids}:
        raise ValueError("Incomplete quality coverage.")
    measured = [r for r in records if not r["warmup"]]
    for q in quality:
        if q.get("error") or any(type(q[d]) is not int or not 1 <= q[d] <= 5 for d in analyze.QUALITY_DIMS):
            raise ValueError("Invalid quality scores.")
        record = next(r for r in measured if r["arm"] == q["arm"] and r["question_id"] == q["question_id"] and r["iteration"] == q["iteration"])
        if record["response_chars"] > 6000:
            if q.get("evaluation_revision") != "fulltext-v2" or q.get("response_sha256") != record["response_sha256"]:
                raise ValueError("A long answer was not re-evaluated in full.")

    pricing = analyze.load_pricing(ROOT / "config" / "pricing.json")
    analyze._REGISTRY = registry
    for r in records:
        r["cost_usd"] = analyze.cost_for(pricing, r["model_requested"], r["prompt_tokens"], r["cached_tokens"], r["completion_tokens"])
        if r["cost_usd"] is None:
            raise ValueError("Missing price for a tested model.")
        r["total_tokens"] = r["prompt_tokens"] + r["completion_tokens"]
        r["visible_output_tokens_estimate"] = r["completion_tokens"] - r["reasoning_tokens"]
    for name, rows in ((f"direct_{RUN}.metrics.jsonl", records), (f"quality_fulltext_{RUN}.jsonl", quality)):
        (OUTPUT / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8", newline="\n")
    provenance = dict(evidence["provenance"])
    provenance["archive_variant"] = archive_variant
    provenance["committed_copies_sha256"] = {
        name: hashlib.sha256((OUTPUT / name).read_bytes()).hexdigest()
        for name in (f"evidence_{RUN}.json.xz", f"raw_fulltext/direct_{RUN}.jsonl", f"quality_fulltext_{RUN}.jsonl")
        if (OUTPUT / name).exists()
    }
    provenance["note"] = (
        "raw_sha256/quality_sha256 were recorded on the benchmark VM before export. The committed "
        "raw_fulltext copy has endpoint_host replaced by a placeholder (scripts/redact_endpoint.py); "
        "per-record response_sha256 values are unchanged and are re-verified by scripts/verify_fulltext.py."
    )
    (OUTPUT / f"provenance_{RUN}.json").write_text(json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    arm_rows, scenario_rows, session_rows = [], [], []
    for arm in sorted(expected_arms):
        group = [r for r in measured if r["arm"] == arm]
        scores = [q for q in quality if q["arm"] == arm]
        arm_rows.append({
            "arm": arm, "requests": len(group),
            "ttft_p50_ms": analyze.pct([r["ttft_ms"] for r in group], 50),
            "ttft_p90_ms": analyze.pct([r["ttft_ms"] for r in group], 90),
            "ttft_p95_ms": analyze.pct([r["ttft_ms"] for r in group], 95),
            "e2e_p50_ms": analyze.pct([r["e2e_ms"] for r in group], 50),
            "e2e_p90_ms": analyze.pct([r["e2e_ms"] for r in group], 90),
            "e2e_p95_ms": analyze.pct([r["e2e_ms"] for r in group], 95),
            # Client-observed streaming decode: first text delta -> last text delta, per visible token.
            "tpot_p50_ms": analyze.pct([r["tpot_ms"] for r in group if r["tpot_ms"] is not None], 50),
            "tpot_p90_ms": analyze.pct([r["tpot_ms"] for r in group if r["tpot_ms"] is not None], 90),
            "decode_tps_p50": analyze.pct([r["decode_tps"] for r in group if r["decode_tps"] is not None], 50),
            "decode_span_lt50ms_requests": sum(r["decode_ms"] is not None and r["decode_ms"] < 50 for r in group),
            "input_tokens_mean": mean(group, "prompt_tokens"),
            "cached_tokens_mean": mean(group, "cached_tokens"),
            "output_tokens_mean": mean(group, "completion_tokens"),
            "reasoning_tokens_mean": mean(group, "reasoning_tokens"),
            "total_tokens_mean": mean(group, "total_tokens"),
            "input_tokens_sum": sum(r["prompt_tokens"] for r in group),
            "output_tokens_sum": sum(r["completion_tokens"] for r in group),
            "reasoning_tokens_sum": sum(r["reasoning_tokens"] for r in group),
            "cost_usd_sum": sum(r["cost_usd"] for r in group),
            "cost_usd_per_1000_requests": mean(group, "cost_usd") * 1000,
            "judge_quality_mean": mean(scores, "quality_mean"),
            "judge_n": len(scores),
        })
        for scenario in SCENARIOS:
            sample = [r for r in group if r["scenario"] == scenario]
            scores = [q for q in quality if q["arm"] == arm and q["scenario"] == scenario]
            scenario_rows.append({
                "arm": arm, "scenario": scenario, "requests": len(sample),
                "ttft_p50_ms": analyze.pct([r["ttft_ms"] for r in sample], 50),
                "e2e_p50_ms": analyze.pct([r["e2e_ms"] for r in sample], 50),
                "total_tokens_mean": mean(sample, "total_tokens"),
                "reasoning_tokens_mean": mean(sample, "reasoning_tokens"),
                "cost_usd_per_1000_requests": mean(sample, "cost_usd") * 1000,
                "judge_quality_mean": mean(scores, "quality_mean"),
                "judge_n": len(scores),
            })
    for r in measured:
        session_rows.append({k: r[k] for k in (
            "run_id", "arm", "scenario", "question_id", "iteration",
            "prompt_tokens", "cached_tokens", "completion_tokens", "reasoning_tokens",
            "total_tokens", "cost_usd", "ttft_ms", "e2e_ms", "status", "response_sha256",
        )})
    write_csv(OUTPUT / "arm_summary.csv", arm_rows)
    write_csv(OUTPUT / "scenario_summary.csv", scenario_rows)
    write_csv(OUTPUT / "single_turn_sessions.csv", session_rows)
    print(json.dumps({"arms": arm_rows, "all_requests": len(records), "measured_requests": len(measured),
                      "all_input_tokens": sum(r["prompt_tokens"] for r in records),
                      "all_output_tokens": sum(r["completion_tokens"] for r in records),
                      "all_reasoning_tokens": sum(r["reasoning_tokens"] for r in records),
                      "all_cost_usd": sum(r["cost_usd"] for r in records)}, ensure_ascii=False, indent=2))

    by_arm = {a["arm"]: a for a in arm_rows}
    luna = by_arm["gpt-5.6-luna@none"]
    mini = by_arm["gpt-5-mini@minimal"]
    mini_high = by_arm["gpt-5-mini@high"]
    luna_high = by_arm["gpt-5.6-luna@high"]
    report = [
        "# Qira 三模型 × Reasoning Effort 实测结果",
        "",
        "测试日期：2026-09-09；正式 run：`20260909_120534`。这是场景可行性实验，不是生产并发容量压测。",
        "",
        "## 结论先行（只对本轮构造样例成立）",
        "",
        "- **最低首字延迟/最低成本候选：GPT-4o mini**。可先用于简单建议、摘要等低成本路径，但本轮写作和创作意图评分低于 Luna，需按场景验收。",
        f"- **综合候选：Luna `none`**。千次成本 ${luna['cost_usd_per_1000_requests']:.4f}，比 GPT-5 mini `minimal` 低 {100*(1-luna['cost_usd_per_1000_requests']/mini['cost_usd_per_1000_requests']):.1f}%；但首字约慢 {(luna['ttft_p50_ms']-mini['ttft_p50_ms'])/1000:.3f} 秒。不能只看首字速度或只看单价。",
        f"- Luna `high` 相比 `none` 成本增加 {100*(luna_high['cost_usd_per_1000_requests']/luna['cost_usd_per_1000_requests']-1):.1f}%，自动质量均分只增加 {luna_high['judge_quality_mean']-luna['judge_quality_mean']:.3f}/5；不足以据此推荐全量开启高档。",
        f"- GPT-5 mini `high` 相比 `minimal`：千次成本 {mini_high['cost_usd_per_1000_requests']/mini['cost_usd_per_1000_requests']:.2f} 倍，TTFT 中位数 {mini_high['ttft_p50_ms']/mini['ttft_p50_ms']:.1f} 倍。本轮交互场景不建议默认 `high`。",
        "- **不要把 effort 越高等同于性价比越好。** 按绝对质量门槛、首字/完成时延预算和真实请求量选择，再用新增的复杂样例验证是否需要升级。",
        "",
        "| 场景 | 下一轮优先验证的配置 | 说明 |",
        "|---|---|---|",
        "| Next Move | GPT-4o mini；Luna none 作质量对照 | 先保证短建议首字与成本，复杂上下文另测 |",
        "| Write For Me | Luna none | 本轮写作评分高于 4o mini/minimal；高档收益需更多复杂样例证明 |",
        "| Catch Me Up | GPT-4o mini | 本轮 3 题摘要评分已高，不能外推长上下文与复杂冲突消解 |",
        "| Pay Attention 文本处理 | Luna none；GPT-5 mini minimal 作低首字时延对照 | STT 与实时语音链路未测 |",
        "| Live Interaction 文本代理 | 4o mini / 5 mini minimal 优先测实时约束 | Luna 更高文本评分不代表真实语音端到端更快 |",
        "| Creator Zone 提示词/意图 | Luna none | 不代表图片生成或编辑质量 |",
        "",
        "## 1. 已验证的测试矩阵",
        "",
        "- GPT-5.6 Luna：none、low、medium、high、xhigh、max（6 组）。",
        "- GPT-5 mini：minimal、low、medium、high（4 组）。",
        "- GPT-4o mini：不发送 reasoning 参数（1 组；部署名 gpt-4o-mini-bench）。",
        "- 17 个构造样例覆盖 Qira 6 个场景；11 × 17 ×（1 次预热 + 3 次测量）= 748 次。",
        "- 748/748 completed，0 API 错误、0 截断、0 空答案；561 次测量，每组合 51 次。",
        "- 每个场景只测文本侧原生能力，不挂载搜索或其他工具。评分与性能采集分开。",
        "",
        "## 2. 环境与边界",
        "",
        "- Sweden Central Linux VM → 同区 AOAI 资源；3 个部署均为 GlobalStandard / PAYGO。",
        "- VM 区域由 Azure IMDS 再次核验；正式记录的 TCP 连接基线为 7.74 ms。",
        "- **GlobalStandard 可能跨区执行推理**：资源所在区相同不等于推理 GPU 物理同区。",
        "- 同区域发起请求能减少客户端网络影响，但 TTFT 仍含网络、排队、prefill、首 token，推理模型还可能含首字前 reasoning；不能称为纯 prefill。",
        "- 运行顺序按组合串行；并发=1。未随机交错测试时段，P95 为小样本描述，不是生产 SLA。",
        "- 输出上限对同一题的所有组合一致（原题上限 + 8192），未按 effort 改变。",
        "- 流式 chunk 未必等于一个 token；现有 decode/TPOT 仅为客户端估计，不作为精确 GPU 解码指标。",
        "",
        "## 3. 价格口径",
        "",
        "来源：[Azure 官方定价页面](https://azure.microsoft.com/en-us/pricing/details/azure-openai/)，2026-09-09 浏览器渲染核验，USD / 1M Token。",
        "",
        "| 模型（Global） | 输入 | 缓存读取 | 输出 |",
        "|---|---:|---:|---:|",
        "| GPT-5.6 Luna，短上下文 | $0.20 | $0.02 | $1.20 |",
        "| GPT-5 mini | $0.25 | $0.03 | $2.00 |",
        "| GPT-4o mini | $0.15 | $0.075 | $0.60 |",
        "",
        "`成本 = ((输入 − 缓存读取) × 输入价 + 缓存读取 × 缓存价 + 输出 × 输出价) / 1,000,000`。",
        "reasoning_tokens 已包含在 output_tokens 中，不能重复相加计费。",
        "Luna 还列有缓存写入价 $0.25 / 1M；旧采集器未单独记录 cache_write_tokens，以下为已采集 usage 的公开价估算，不是账单。",
        "",
        "## 4. 11 组结果",
        "",
        "Token、成本为每次测量请求均值；TTFT/E2E 为中位数。各组使用相同 17 题，不代表客户真实流量权重。",
        "",
        "| 模型@effort | TTFT P50 s | TTFT P95 s | E2E P50 s | 输入 Token | 输出 Token | 其中推理 | 总 Token/单轮 | $/千次 | 质量/5 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for a in arm_rows:
        report.append(
            f"| {a['arm']} | {a['ttft_p50_ms']/1000:.3f} | {a['ttft_p95_ms']/1000:.3f} | "
            f"{a['e2e_p50_ms']/1000:.3f} | {a['input_tokens_mean']:.1f} | {a['output_tokens_mean']:.1f} | "
            f"{a['reasoning_tokens_mean']:.1f} | {a['total_tokens_mean']:.1f} | "
            f"{a['cost_usd_per_1000_requests']:.4f} | {a['judge_quality_mean']:.2f} |"
        )
    total_bursts = sum(a["decode_span_lt50ms_requests"] for a in arm_rows)
    report.extend([
        "",
        "### 4.1 分位数与流式解码（客户端观测）",
        "",
        f"客户端 TPOT = 首个文本块到最后一个文本块的跨度 ÷（可见输出 token − 1）；tok/s 为其倒数。本轮 {len(measured)} 次测量中有 {total_bursts} 次首末文本块跨度小于 50 ms（即整段近乎一次到达），"
        "流式为逐块到达，TPOT 可作为客户端观测到的生成节奏；但 chunk 未必等于一个 token，且并发=1，不是 GPU 解码指标或系统吞吐。",
        "",
        "| 模型@effort | TTFT P50/P90/P95 s | E2E P50/P90/P95 s | TPOT P50/P90 ms | tok/s P50 | 首末<50 ms |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for a in arm_rows:
        report.append(
            f"| {a['arm']} | {a['ttft_p50_ms']/1000:.3f} / {a['ttft_p90_ms']/1000:.3f} / {a['ttft_p95_ms']/1000:.3f} | "
            f"{a['e2e_p50_ms']/1000:.3f} / {a['e2e_p90_ms']/1000:.3f} / {a['e2e_p95_ms']/1000:.3f} | "
            f"{a['tpot_p50_ms']:.2f} / {a['tpot_p90_ms']:.2f} | {a['decode_tps_p50']:.0f} | {a['decode_span_lt50ms_requests']}/{a['requests']} |"
        )
    report.extend([
        "",
        "## 5. 各场景对比",
        "",
        "质量：同一个 judge-terra 盲评，5 维 1–5 分，每个 arm×题只评第 1 个非预热答案，共 187 份。",
        "WFM01 全部 11 个组合使用完整长文重新评分；不再用前 6000 字代替全文。",
        "自动评分仅作初筛：隐藏模型名无法消除同家族偏好、措辞偏好和裁判随机性；小分差不等于显著优势。",
        "",
    ])
    for scenario in SCENARIOS:
        report.extend([f"### {scenario}", "", "| 组合 | TTFT P50 s | E2E P50 s | Token/单轮 | $/千次 | 质量/5 |", "|---|---:|---:|---:|---:|---:|"])
        for row in scenario_rows:
            if row["scenario"] == scenario:
                report.append(f"| {row['arm']} | {row['ttft_p50_ms']/1000:.3f} | {row['e2e_p50_ms']/1000:.3f} | {row['total_tokens_mean']:.1f} | {row['cost_usd_per_1000_requests']:.4f} | {row['judge_quality_mean']:.2f} |")
        report.append("")
    report.extend([
        "## 6. 会话 Token 与总体消耗",
        "",
        "**当前每个样例是独立单轮会话**，不是带历史回传的多轮 session。LI02 也没有继承 LI01 的实际模型回答，不能把单轮均值冒充完整多轮会话成本。",
        "单轮会话总量 = API input_tokens + output_tokens；缓存读取已包含在输入，reasoning 已包含在输出。",
        "多轮真实会话应逐轮相加 API usage（含再次送入的历史），本轮未测量这种增量。",
        "",
        f"- 正式 748 次（含预热）：输入 {sum(r['prompt_tokens'] for r in records):,}；输出 {sum(r['completion_tokens'] for r in records):,}；其中推理 {sum(r['reasoning_tokens'] for r in records):,}。",
        f"- 总 Token {sum(r['total_tokens'] for r in records):,}；估算模型调用费 **${sum(r['cost_usd'] for r in records):.6f}**。",
        "- 此金额不包含废弃首轮、effort 探活、诊断、judge 调用、VM/磁盘/网络等费用。",
        "",
        "## 7. 可复核文件与保留",
        "",
        "- [561 次单轮明细](single_turn_sessions.csv)、[11 组合汇总](arm_summary.csv)、[66 场景×组合汇总](scenario_summary.csv)。",
        f"- [完整数值 JSONL](direct_{RUN}.metrics.jsonl)、[187 份评分](quality_fulltext_{RUN}.jsonl)、[来源与校验](provenance_{RUN}.json)。",
        f"- [压缩证据包](evidence_{RUN}.json.xz)：保留全部数值字段和每份输出的 SHA256；不含生成全文/预览。",
        "- 生成全文已于 2026-09-09 通过 scp 回收到 [raw_fulltext/](raw_fulltext/)（VM 上原始文件 SHA256 `77c04776…3059888`；仓库内副本已把 endpoint_host 替换为占位符，748 条 response_text 与数值记录中的 response_sha256 仍逐条一致，可用 `python scripts/verify_fulltext.py` 复核；另含首版评分 `quality_20260909_120534.jsonl`）。首轮废弃 run 只在线下保留，不入库。VM 磁盘不再是唯一副本。",
        "- [资源收尾记录](resource_closeout.json)：VM 已 deallocated（关机不删除），临时 SSH 入站规则和空临时存储账号已移除。磁盘/静态公网 IP 仍可能计费；模型部署保留供复核，未继续发起测试请求。",
        "- Pay Attention 只评估已提供转写文本后的摘要/翻译；Live Interaction 是文本代理；Creator Zone 是提示词与编辑意图，未测 STT、音视频或图片生成质量。",
        "",
    ])
    (OUTPUT / "Qira-实测结果-20260909.md").write_text("\n".join(report), encoding="utf-8", newline="\n")
    print("VERIFIED: 748 performance rows, 561 measured single-turn sessions, 187 quality rows, 66 scenario-arm rows.")


if __name__ == "__main__":
    main()
