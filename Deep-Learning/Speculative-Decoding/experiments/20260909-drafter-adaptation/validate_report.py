"""Validate the drafter-adaptation experiment: exported files, recomputed summary, README blocks.

Offline only. Reads the exported result files, recomputes ``data/summary.json``
through ``analyze_results.summarize`` and requires equality, checks the README's
generated table blocks against that summary, and verifies every published file
against ``evidence/files.json``.
"""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re

from analyze_results import digest_file, dump_json, read_json, require, summarize


ROOT = Path(__file__).resolve().parent
MANIFEST = "evidence/files.json"
RULES = "evidence/rule-results.json"
IGNORED_PARTS = {"__pycache__", ".venv", ".pytest_cache"}
READMES = {"README.md": False, "README_CN.md": True}
BLOCK = "ADAPTATION_TABLE"
# Shapes that must never appear in published evidence: IPv4 addresses, UUIDs
# (cloud subscription/tenant identifiers) and absolute home, mount or drive paths.
PRIVATE_SHAPES = (
    re.compile(r"\b(?!127\.)(?!0\.0\.0\.0\b)\d{1,3}(?:\.\d{1,3}){3}\b"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    re.compile(r"/home/|/mnt/|/root/|[A-Za-z]:\\\\"),
)


def topic_dir(root):
    return root.parent.parent


def markdown_table(headers, rows):
    return "\n".join("| " + " | ".join(map(str, row)) + " |" for row in [headers, ["---"] * len(headers), *rows])


def interval(entry):
    lower, upper = entry["bootstrap_95_percent_interval"]
    sign = "+" if lower >= 0 else ""
    return f"{sign}{lower:.3f}, {'+' if upper >= 0 else ''}{upper:.3f}"


def adaptation_table(summary, chinese):
    r3, r4 = summary["round3"], summary["round4"]
    seed_labels = ("20260908", "1", "2")
    comparisons = [(f"A / seed {label}", r4["agreement_paired"][f"english_v3_seed{index}_minus_released"])
                   for index, label in enumerate(seed_labels)]
    comparisons.extend((
        ("B / selector", r4["agreement_paired"]["chinese_ours_minus_released_selector"]),
        ("B / argmax", r4["agreement_paired"]["chinese_ours_minus_released_argmax"]),
    ))
    headers = (["设置 / draft model 路径", "发布版 / 再训", "差值的 95% 区间", "成对提示"] if chinese else
               ["Setting / draft path", "Released / adapted", "95% interval of difference", "Paired prompts"])
    sections = []
    for metric, title, places in (
        ("first_offset_hit_rate", "#### 首位命中率" if chinese else "#### First-Offset Agreement", 3),
        ("joint_prefix_acceptance_length", "#### 联合前缀接受长度" if chinese else "#### Joint-Prefix Acceptance Length", 2),
    ):
        rows = []
        for label, comparison in comparisons:
            entry = comparison[metric]
            rows.append([label, f"{entry['reference']:.{places}f} / {entry['candidate']:.{places}f}",
                         interval(entry), comparison["paired_prompts"]])
        sections.extend([title, markdown_table(headers, rows)])
    sections.append(
        "每种语言请求 200 条提示；表中显示实际可成对评估数，短输出的排除项仍在记录中。A 使用 selector；B 的 argmax 行是在相同训练权重上禁用 selector 的诊断，不是单独的训练消融。五组比较、每组两项指标，共十个区间；按提示 bootstrap 2,000 次，未作多重比较修正。" if chinese else
        "Each language requested 200 prompts; the table shows evaluable pairs, with short-output exclusions retained in the records. A uses the selector. B's argmax row disables it on the same trained weights; this is not a training ablation. Five comparisons with two metrics give ten intervals, using 2,000 prompt-level bootstrap resamples without multiplicity correction.")
    sections.append("#### vLLM 服务测量" if chinese else "#### vLLM Serving Measurements")
    sections.append(
        "同一个微调目标分别配不开推测、发布版 draft model、再训 draft model，每条路线只执行一次；每档并发测 40 条提示，`max_tokens=256`。A 的服务测试只覆盖 seed 20260908，另两个种子未做服务测试。" if chinese else
        "Each fine-tuned target is served without speculation, with the released draft model and with the adapted draft model, once per route. Each concurrency level measures 40 prompts with `max_tokens=256`. A's serving run covers only seed 20260908; the other two seeds were not serving-tested.")
    serving_headers = ["指标" if chinese else "Metric", "A / en", "B / zh"]
    rows = []
    for concurrency in ("1", "4"):
        cells = []
        for setting, adapted in ((r3, "dflash_v3"), (r4, "dflash_ours")):
            cells.append(" / ".join(f"{setting['vllm'][route]['levels'][concurrency]['tokens_per_second']:.1f}"
                                    for route in ("baseline", "dflash_released", adapted)))
        rows.append([("吞吐 tok/s，并发 " if chinese else "Throughput tok/s, concurrency ") + concurrency, *cells])
    cells = []
    for setting, adapted in ((r3, "dflash_v3"), (r4, "dflash_ours")):
        cells.append(" / ".join(f"{setting['vllm_server_acceptance'][route]['derived_mean_acceptance_length']:.2f}"
                                for route in ("dflash_released", adapted)))
    rows.append(["服务端接受长度" if chinese else "Server acceptance length", *cells])
    sections.append(markdown_table(serving_headers, rows))
    sections.append(
        "吞吐列依次为不开推测 / 发布版 / 再训；接受长度列为发布版 / 再训。接受长度由日志累计 accepted/drafted 推导，含预热与两档并发，不是同一个测量分母。服务性能不作显著性声明；答案质量未评分。" if chinese else
        "Throughput cells list no speculation / released / adapted; acceptance cells list released / adapted. Acceptance is derived from cumulative logged accepted/drafted counts, including warmup and both concurrency levels, so its denominator differs. No serving-significance claim is made; answer quality was not graded.")
    sections.extend(round5_section(summary["round5"], chinese))
    return "\n\n".join(sections)


def round5_section(r5, chinese):
    """Repeated serving runs of setting B: four observations per cell, reported as raw ranges."""
    routes = r5["vllm"]["routes"]
    comparison = r5["vllm"]["adapted_vs_released"]
    identity = r5["text_identity"]
    sections = ["#### 设置 B 服务复测（Round 5）" if chinese else "#### Setting B Serving Re-test (Round 5)"]
    sections.append(
        "2026-09-13 在新的 VM 会话与新的 vLLM 安装上重跑设置 B 的三条服务路线：同一份权重、同一 40 条提示、同一 seed 与引擎参数。每条路线启动两次 server（A 轮顺序不开推测→发布版→再训，B 轮反序），每次 server 内跑两遍客户端，每格共 4 个吞吐观测。下表给出均值与原始极差，不假设分布。" if chinese else
        "On 2026-09-13 the three serving routes of setting B were re-run in a fresh VM session with a fresh vLLM install: same weights, same 40 prompts, same seed and engine flags. Each route started the server twice (pass A ordered no speculation → released → adapted, pass B reversed) and ran the client twice per server, giving four throughput observations per cell. The table reports means with raw ranges and makes no distributional assumption.")
    headers = (["并发", "不开推测", "发布版", "再训", "再训 / 发布版", "极差重叠"] if chinese else
               ["Concurrency", "No speculation", "Released", "Adapted", "Adapted / released", "Ranges overlap"])
    rows = []
    for concurrency in sorted(comparison, key=int):
        cells = [f"{routes[route]['levels'][concurrency]['mean']:.1f} [{routes[route]['levels'][concurrency]['min']:.1f}–{routes[route]['levels'][concurrency]['max']:.1f}]"
                 for route in ("baseline", "dflash_released", "dflash_ours")]
        gain = comparison[concurrency]["adapted_over_released_mean_pct"]
        overlap = comparison[concurrency]["ranges_overlap"]
        rows.append([concurrency, *cells, f"{'+' if gain >= 0 else ''}{gain:.1f}%", ("是" if overlap else "否") if chinese else ("yes" if overlap else "no")])
    sections.append(markdown_table(headers, rows))
    c1 = identity["1"]
    rel_vs_ours = min(c1["released_vs_adapted"].values())
    base_vs_rel = c1["baseline_vs_released"]
    same_route = c1["same_route_across_server_starts"]
    span = lambda values: str(min(values)) if min(values) == max(values) else f"{min(values)}–{max(values)}"
    sections.append(
        (f"吞吐单位 tok/s，每格格式为均值 [最小–最大]，4 次观测。这些观测是同一确定性 greedy 解码的计时重复，极差表示测量抖动，不表示提示集抽样方差。同一 40 条提示仍是单一样本。"
         f"并发 1 的逐字一致性（按 `text_sha256`）：发布版与再训 draft model 的输出在 4 次运行中均为 {rel_vs_ours}/{c1['prompts']} 相同；不开推测与发布版推测解码的输出为 {span(base_vs_rel.values())}/{c1['prompts']} 相同；同一路线在两次 server 启动间为 "
         + "、".join(f"{same_route[route]}/{c1['prompts']}" for route in ("baseline", "dflash_released", "dflash_ours")) +
         " 相同。并发 4 与 8 的对应计数在 [汇总文件](experiments/20260909-drafter-adaptation/data/summary.json) 的 `round5.text_identity` 中。") if chinese else
        (f"Throughput in tok/s; each cell is mean [min–max] over 4 observations. The observations are timing repeats of the same deterministic greedy decode, so the ranges bound measurement jitter, not prompt-set sampling variance; the 40 prompts remain a single sample. "
         f"Byte identity at concurrency 1 (by `text_sha256`): released and adapted draft models produce {rel_vs_ours}/{c1['prompts']} identical outputs in every run; no-speculation and released speculative decoding agree on {span(base_vs_rel.values())}/{c1['prompts']}; the same route across the two server starts agrees on "
         + ", ".join(f"{same_route[route]}/{c1['prompts']}" for route in ("baseline", "dflash_released", "dflash_ours")) +
         ". Concurrency 4 and 8 counts are in `round5.text_identity` of the [summary](experiments/20260909-drafter-adaptation/data/summary.json)."))
    return sections


def training_loss_table(summary, chinese):
    headers = (["训练运行", "步数 / 窗口", "Backbone loss：首 / 尾", "Selector loss：首 / 尾"] if chinese else
               ["Training run", "Steps / window", "Backbone loss: first / last", "Selector loss: first / last"])
    rows = []
    for name, entry in summary["training"].items():
        language = ("中文" if chinese else "Chinese") if name.startswith("chinese") else ("英文" if chinese else "English")
        label = f"{language} seed {entry['args']['seed']}"
        link = f"experiments/20260909-drafter-adaptation/{entry['history_path']}"
        rows.append([f"[{label}]({link})", f"{entry['steps']} / {entry['window_steps']}",
                     f"{entry['backbone_loss_first_window']:.4f} / {entry['backbone_loss_last_window']:.4f}",
                     f"{entry['selector_loss_first_window']:.4f} / {entry['selector_loss_last_window']:.4f}"])
    return markdown_table(headers, rows)


def generated_block(text, key, body, *, refresh):
    start, end = f"<!-- BEGIN {key} -->", f"<!-- END {key} -->"
    require(text.count(start) == 1 and text.count(end) == 1, "MISSING_OR_DUPLICATE_REPORT_BLOCK:" + key)
    before, rest = text.split(start)
    old, after = rest.split(end)
    expected = "\n" + body + "\n"
    if not refresh:
        require(old == expected, "REPORT_DATA_DRIFT:" + key)
    return before + start + expected + end + after


def loss_source_excerpts(text, source, chinese, *, refresh):
    heading = "### 损失函数与 selector" if chinese else "### Loss Functions and the Selector"
    require(text.count(heading) == 1, "LOSS_DOCUMENTATION_MISSING")
    before, section = text.split(heading)
    section, separator, after = section.partition("\n### ")
    lines = source.splitlines()
    excerpts = []
    for first, last in (("    per_token = ", "    return backbone, selector"),
                        ("        pairwise = ", "        scores = ")):
        start = next(index for index, line in enumerate(lines) if line.startswith(first))
        end = next(index for index in range(start, len(lines)) if lines[index].startswith(last))
        excerpts.append("\n".join(lines[start:end + 1]) + "\n")
    pattern = r"^```python\n(.*?)^```"
    existing = re.findall(pattern, section, re.S | re.M)
    require(len(existing) == len(excerpts), "LOSS_SOURCE_EXCERPT_MISSING")
    if refresh:
        expected = iter(excerpts)
        section = re.sub(pattern, lambda match: "```python\n" + next(expected) + "```", section, flags=re.S | re.M)
    else:
        require(existing == excerpts, "LOSS_SOURCE_EXCERPT_DRIFT")
    return before + heading + section + separator + after


def published_files(root):
    return sorted(path for path in root.rglob("*") if path.is_file()
                  and not set(path.relative_to(root).parts) & IGNORED_PARTS
                  and path.suffix not in {".pyc", ".pyo"}
                  and path.relative_to(root).as_posix() not in {MANIFEST, RULES})


def file_manifest(root):
    records = {}
    for path in published_files(root):
        require(not path.is_symlink() and path.resolve().is_relative_to(root.resolve()), "PUBLISHED_SYMLINK")
        records[path.relative_to(root).as_posix()] = {"bytes": path.stat().st_size, "sha256": digest_file(path)}
    return {"files": records, "scope": "Published experiment files; generated rule results and this manifest excluded."}


def verify_manifest(root):
    saved = read_json(root / MANIFEST)
    for name in saved["files"]:
        relative = PurePosixPath(name)
        require(not relative.is_absolute() and ".." not in relative.parts and "\\" not in name and ":" not in name, "MANIFEST_PATH_ESCAPE")
    require(saved == file_manifest(root), "PUBLISHED_FILE_HASH_OR_SET_MISMATCH")


def verify_provenance(root):
    provenance = read_json(root / "evidence/provenance.json")
    for round_name in ("round3", "round4", "round5"):
        record = provenance[round_name]
        for public, entry in record["results"].items():
            require((root / "results" / round_name / public).is_file(), "PROVENANCE_RESULT_MISSING:" + public)
        for public, entry in record["source"].items():
            if entry.get("published", True):
                require((root / "source" / round_name / public).is_file(), "PROVENANCE_SOURCE_MISSING:" + public)
        for public, entry in record["inputs"].items():
            require((root / "inputs" / round_name / public).is_file(), "PROVENANCE_INPUT_MISSING:" + public)
        for public, entry in record["artifacts"].items():
            require(entry["published"] is False and len(entry["sha256"]) == 64, "ARTIFACT_PROVENANCE_INCOMPLETE:" + public)
        for name, entry in record["logs"].items():
            path = root / "logs" / round_name / (name + ".log")
            require(entry.get("published") and path.is_file(), "PUBLISHED_LOG_MISSING:" + name)
            require(digest_file(path) == entry["published_sha256"], "PUBLISHED_LOG_HASH_MISMATCH:" + name)
            require(len(path.read_text(encoding="utf-8").splitlines()) == len(entry["source_line_numbers"]),
                    "PUBLISHED_LOG_LINE_COUNT_MISMATCH:" + name)
    history = read_json(root / "results/round4/training/drafter_zh_history.json")
    logged_steps = []
    for line in (root / "logs/round4/round4.log").read_text(encoding="utf-8").splitlines():
        if not line.startswith("{"):
            continue
        item = json.loads(line)
        if "loss_50" not in item:
            continue
        step = item["step"]
        require(50 <= step <= len(history["history"]) and step % 50 == 0, "TRAINING_LOG_STEP_INVALID")
        for field, logged in (("history", "loss_50"), ("selector_history", "selector_loss_50")):
            expected = round(sum(history[field][step - 50:step]) / 50, 4)
            require(item[logged] == expected, "TRAINING_LOG_WINDOW_MISMATCH:" + field)
        logged_steps.append(step)
    require(logged_steps == list(range(50, len(history["history"]) + 1, 50)), "TRAINING_LOG_STEPS_MISSING")
    figures = read_json(root / "images/loss-figures.json")
    for name, checksum in figures["sources"].items():
        require(digest_file(root / name) == checksum, "TRAINING_FIGURE_SOURCE_MISMATCH:" + name)
    for name, entry in figures["figures"].items():
        require(digest_file(root / "images" / name) == entry["sha256"], "TRAINING_FIGURE_HASH_MISMATCH:" + name)
    for text_path in root.rglob("*"):
        if not text_path.is_file() or text_path.suffix not in {".json", ".jsonl", ".log"}:
            continue
        if set(text_path.relative_to(root).parts) & IGNORED_PARTS:
            continue
        text = text_path.read_text(encoding="utf-8")
        for shape in PRIVATE_SHAPES:
            require(shape.search(text) is None, "PRIVATE_MARKER_IN_PUBLIC_FILE:" + text_path.name)


def validate(root=ROOT, *, refresh=False):
    root = root.resolve()
    summary = summarize(root)
    if refresh:
        dump_json(root / "data/summary.json", summary)
    require(read_json(root / "data/summary.json") == summary, "SAVED_SUMMARY_MISMATCH")
    verify_provenance(root)
    for filename, chinese in READMES.items():
        path = topic_dir(root) / filename
        text = path.read_text(encoding="utf-8")
        text = generated_block(text, BLOCK, adaptation_table(summary, chinese), refresh=refresh)
        text = generated_block(text, "TRAINING_LOSS", training_loss_table(summary, chinese), refresh=refresh)
        source = (root / "source/round4/train_drafter.py").read_text(encoding="utf-8")
        text = loss_source_excerpts(text, source, chinese, refresh=refresh)
        if refresh:
            path.write_text(text, encoding="utf-8")
        require(f"python experiments/{root.name}/validate_report.py" in text, "REPLAY_ENTRY_MISSING:" + filename)
    if refresh:
        dump_json(root / MANIFEST, file_manifest(root))
    verify_manifest(root)
    records = [{"id": name, "status": "PASS", "evidence": evidence} for name, evidence in (
        ("summary-recomputed-from-per-request-records", ["results/", "data/summary.json"]),
        ("paired-bootstrap-only-on-identical-target-text", ["results/round4/agreement/", "results/round3/agreement/"]),
        ("published-prompts-hash-to-recorded-inputs", ["inputs/", "results/round4/acceptance/", "results/round3/acceptance/"]),
        ("training-history-and-readable-log-integrity", ["results/round3/training/", "results/round4/training/", "logs/"]),
        ("round5-repeated-serving-observations-and-text-identity", ["results/round5/vllm/", "logs/round5/"]),
        ("provenance-hashes-and-private-marker-scan", ["evidence/provenance.json"]),
        ("generated-bilingual-adaptation-table", ["../../README.md", "../../README_CN.md"]),
        ("published-file-integrity", [MANIFEST]),
    )]
    result = {"scope": "Offline verification of exported evidence; not fresh GPU execution, regrading or a quality certification.",
              "checks": records, "manifest_sha256": digest_file(root / MANIFEST)}
    if refresh:
        dump_json(root / RULES, result)
    require(read_json(root / RULES) == result, "VALIDATION_RECORD_DRIFT")
    for record in records:
        print("RULE", record["id"], record["status"])
    print("ADAPTATION_GATE=PASS")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", action="store_true", help="Regenerate summary, README block and manifest after reviewing an edit")
    args = parser.parse_args()
    validate(refresh=args.refresh)


if __name__ == "__main__":
    main()
