"""Project the author's private drafter-adaptation archive into public evidence.

The private archive holds raw run directories with host names, absolute paths and
full generated text. This exporter copies only an allowlist of result files, removes
host and path fields, truncates generated text to a short head, and records the
SHA-256 of every source file so the public projection can be traced back without
redistributing the private material.

Run manually by the author; never runs in CI. Use ``analyze_results.py`` and
``validate_report.py`` on the exported files.
"""

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import re

from analyze_results import dump_json, require, training_statistics


ROOT = Path(__file__).resolve().parent
HOST_FIELDS = ("host", "gpu_name", "base_url")
PATH_FIELDS = ("target", "adapter", "drafter", "drafter_path", "prompts", "prompts_file", "output", "data")
TEXT_HEAD = 80
ROUND3_RESULT_FILES = {
    "acceptance/released_selector.json": "results/r3_acc_released_selector.json",
    "acceptance/released_argmax.json": "results/r3_acc_released_argmax.json",
    "acceptance/v3_selector.json": "results/r3_acc_v3_selector.json",
    "acceptance/v3_argmax.json": "results/r3_acc_v3_argmax.json",
    "agreement/released_selector.json": "results/r3_pred_released_selector.json",
    "agreement/released_argmax.json": "results/r3_pred_released_argmax.json",
    "agreement/v3_selector.json": "results/r3_pred_v3_selector.json",
    "agreement/v3_argmax.json": "results/r3_pred_v3_argmax.json",
    "hf-throughput.json": "results/r3_hf_throughput.json",
    "vllm/baseline.json": "results/r3_vllm_baseline.json",
    "vllm/dflash_released.json": "results/r3_vllm_dflash_released.json",
    "vllm/dflash_v3.json": "results/r3_vllm_dflash_v3.json",
    "gates/target_v2.json": "results/degeneration_target_v2.json",
    "gates/base_target.json": "results/degeneration_base.json",
    "training/adapter_v2_summary.json": "checkpoints/adapter-v2/training_summary.json",
    "training/drafter_v3_history.json": "checkpoints/drafter-v3/training-history.json",
}
ROUND4_RESULT_FILES = {
    "agreement/base_zh_released.json": "results/r4_pred_base_zh_released.json",
    "agreement/ftzh_released.json": "results/r4_pred_ftzh_released.json",
    "agreement/ftzh_released_argmax.json": "results/r4_pred_ftzh_released_argmax.json",
    "agreement/ftzh_ours.json": "results/r4_pred_ftzh_ours.json",
    "agreement/ftzh_ours_argmax.json": "results/r4_pred_ftzh_ours_argmax.json",
    "agreement/en200_released.json": "results/r4_pred_en200_released.json",
    "agreement/en200_v3_seed0.json": "results/r4_pred_en200_v3s0.json",
    "agreement/en200_v3_seed1.json": "results/r4_pred_en200_v3s1.json",
    "agreement/en200_v3_seed2.json": "results/r4_pred_en200_v3s2.json",
    "acceptance/ftzh_released.json": "results/r4_acc_ftzh_released.json",
    "acceptance/ftzh_ours.json": "results/r4_acc_ftzh_ours.json",
    "vllm/baseline.json": "results/r4_vllm_zh_baseline.json",
    "vllm/dflash_released.json": "results/r4_vllm_zh_dflash_released.json",
    "vllm/dflash_ours.json": "results/r4_vllm_zh_dflash_ours.json",
    "gates/target_zh.json": "results/r4_degeneration_zh.json",
    "gates/base_target_zh.json": "results/r4_degeneration_base_zh.json",
    "training/adapter_zh_summary.json": "results/r4_finetune_zh_summary.json",
    "training/drafter_zh_history.json": "results/r4_drafter_zh_history.json",
    "training/drafter_v3_seed1_history.json": "out/drafter-v3-seed1/training-history.json",
    "training/drafter_v3_seed2_history.json": "out/drafter-v3-seed2/training-history.json",
}
SOURCE_FILES = {
    "prepare_domain_data.py": "scripts/prepare_domain_data.py",
    "finetune_target.py": "scripts/finetune_target.py",
    "check_degeneration.py": "scripts/check_degeneration.py",
    "generate_responses.py": "scripts/generate_responses.py",
    "train_drafter.py": "scripts/train_drafter.py",
    "analyze_predictability.py": "scripts/analyze_predictability.py",
    "measure_acceptance.py": "scripts/measure_acceptance.py",
    "bench_throughput.py": "scripts/bench_throughput.py",
    "export_drafter_for_vllm.py": "scripts/export_drafter_for_vllm.py",
    "vllm_client_bench.py": "scripts/vllm_client_bench.py",
}
# Orchestration shells embed private host paths; they are hashed, not published.
ORCHESTRATION_FILES = {
    "round3_phase_a.sh": "scripts/round3_phase_a.sh",
    "round3_phase_b.sh": "scripts/round3_phase_b.sh",
    "round3_phase_c.sh": "scripts/round3_phase_c.sh",
}
ROUND4_SOURCE_FILES = {
    "prepare_domain_data.py": "prepare_domain_data.py",
    "finetune_target.py": "finetune_target.py",
    "check_degeneration.py": "check_degeneration.py",
    "generate_responses.py": "generate_responses.py",
    "train_drafter.py": "train_drafter.py",
    "draft_metrics.py": "draft_metrics.py",
    "bench_throughput.py": "bench_throughput.py",
    "analyze_predictability.py": "analyze_predictability.py",
    "measure_acceptance.py": "measure_acceptance.py",
    "export_drafter_for_vllm.py": "export_drafter_for_vllm.py",
    "vllm_client_bench.py": "vllm_client_bench.py",
    "stage0_gradient_canary.py": "stage0_gradient_canary.py",
}
# Held-out prompt sets and split manifests are public-dataset derivatives; published verbatim.
ROUND3_INPUT_FILES = {
    "eval_prompts_en40.jsonl": "data_v2/eval_prompts.jsonl",
    "split_manifest_en.json": "data_v2/split_manifest.json",
}
ROUND4_INPUT_FILES = {
    "eval_prompts_zh200.jsonl": "data_zh/eval_prompts.jsonl",
    "split_manifest_zh.json": "data_zh/split_manifest.json",
    "eval_prompts_en200.jsonl": "data_en/eval_prompts.jsonl",
    "split_manifest_en.json": "data_en/split_manifest.json",
}
ROUND4_ARTIFACT_FILES = {
    "adapter-zh/adapter_model.safetensors": "out/adapter-zh/adapter_model.safetensors",
    "adapter-zh/adapter_config.json": "out/adapter-zh/adapter_config.json",
    "drafter-zh/model.safetensors": "out/drafter-zh/model.safetensors",
    "drafter-zh/config.json": "out/drafter-zh/config.json",
    "drafter-v3-seed1/model.safetensors": "out/drafter-v3-seed1/model.safetensors",
    "drafter-v3-seed2/model.safetensors": "out/drafter-v3-seed2/model.safetensors",
    "data_zh/train.jsonl": "data_zh/train.jsonl",
    "data_zh/corpus.jsonl": "data_zh/corpus.jsonl",
}
ROUND4_LOG_FILES = {
    "round4": "logs/round4-resume.log",
    "round4_initial": "logs/round4-terminal.log",
    "vllm_baseline": "logs/r4_vllm_baseline_server.log",
    "vllm_dflash_released": "logs/r4_vllm_dflash_released_server.log",
    "vllm_dflash_ours": "logs/r4_vllm_dflash_ours_server.log",
}
# Round 5 (2026-09-13) re-served the Round 4 Chinese target and both draft models on a fresh VM
# session: two server starts per route (A: baseline->released->ours, B: reversed) and two client
# passes per server, so every route/concurrency cell has four throughput observations.
ROUND5_ROUTES = ("baseline", "dflash_released", "dflash_ours")
ROUND5_RESULT_FILES = {
    f"vllm/{route}_rep{rep}p{pas}.json": f"results/r5_vllm_{route}_rep{rep}p{pas}.json"
    for route in ROUND5_ROUTES for rep in "AB" for pas in (1, 2)
}
ROUND5_RESULT_FILES["vllm/discovery.json"] = "state/discovery.json"
ROUND5_LOG_FILES = {f"vllm_{route}_rep{rep}": f"logs/r5_server_{route}_rep{rep}.log"
                    for route in ROUND5_ROUTES for rep in "AB"}
# Round 6 (2026-09-14) split the 200 held-out Chinese prompts into five blocks of 40 (block 0 is the
# Round 4/5 set) and served each route once at concurrency 1/4/8/16. A thinking-mode pass on block 0
# is published as well; the analyzer marks it invalid because the client retrieved no text.
ROUND6_BLOCKS = (0, 1, 2, 3, 4)
ROUND6_RESULT_FILES = {
    f"vllm/{route}_b{block}.json": f"results/r6_vllm_{route}_b{block}.json"
    for route in ROUND5_ROUTES for block in ROUND6_BLOCKS
}
ROUND6_RESULT_FILES.update({f"vllm/{route}_think_b0.json": f"results/r6_vllm_{route}_think_b0.json" for route in ROUND5_ROUTES})
ROUND6_RESULT_FILES["vllm/discovery.json"] = "state/discovery.json"
ROUND6_LOG_FILES = {f"vllm_{route}": f"logs/r6_server_{route}.log" for route in ROUND5_ROUTES}
# The client gained --skip and --thinking for Round 6; that version is published, the shell is hashed only.
ROUND6_SOURCE_FILES = {"vllm_client_bench.py": "src/vllm_client_bench.py"}
ARTIFACT_FILES = {
    "adapter-v2/adapter_model.safetensors": "checkpoints/adapter-v2/adapter_model.safetensors",
    "adapter-v2/adapter_config.json": "checkpoints/adapter-v2/adapter_config.json",
    "adapter-v2/training_summary.json": "checkpoints/adapter-v2/training_summary.json",
    "drafter-v3/model.safetensors": "checkpoints/drafter-v3/model.safetensors",
    "drafter-v3/config.json": "checkpoints/drafter-v3/config.json",
    "drafter-v3/training-history.json": "checkpoints/drafter-v3/training-history.json",
    "data/train.jsonl": "data_v2/train.jsonl",
    "data/corpus.jsonl": "data_v2/corpus.jsonl",
    "data/corpus.jsonl.manifest.json": "data_v2/corpus.jsonl.manifest.json",
}
LOG_FILES = {
    "phase_a": "round3/round3_phase_a.log",
    "phase_b": "round3/round3_phase_b.log",
    "phase_c": "round3/round3_phase_c.log",
    "vllm_baseline": "logs/vllm_baseline_server.log",
    "vllm_dflash_released": "logs/vllm_dflash_released_server.log",
    "vllm_dflash_v3": "logs/vllm_dflash_v3_server.log",
}
STAGE_LINE = re.compile(r"^===== (\S+).*?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z) =====$")
TARGET_LOSS_LINE = re.compile(r"^step \d+/\d+ loss [\d.e+-]+ lr [\d.e+-]+ gpu [\d.]+G elapsed \d+s$")
ARCHITECTURE_LINE = re.compile(r"INFO (\d\d-\d\d \d\d:\d\d:\d\d).*?Resolved architecture: (\w+)")
SPEC_LINE = re.compile(r"speculative_config=SpeculativeConfig\((.*?)\)")
SPEC_METRICS = re.compile(r"Mean acceptance length: ([\d.]+), Accepted throughput: [\d.]+ tokens/s, "
                          r"Drafted throughput: [\d.]+ tokens/s, Accepted: (\d+) tokens, Drafted: (\d+) tokens")
# Generic private-identifier shapes; the author's exact host and account strings are
# supplied through PRIVATE_MARKERS_FILE (one per line) and never published.
GENERIC_PRIVATE_PATTERNS = (
    re.compile(r"\b(?!127\.)(?!0\.0\.0\.0\b)\d{1,3}(?:\.\d{1,3}){3}\b"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    re.compile(r"/home/|/mnt/|/root/|[A-Za-z]:\\\\"),
)


def private_markers():
    path = os.environ.get("PRIVATE_MARKERS_FILE")
    if not path:
        return ()
    return tuple(line.strip() for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip())


def find_private(text, markers):
    for pattern in GENERIC_PRIVATE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(0)
    for marker in markers:
        if marker in text:
            return "<private marker>"
    return None


def digest_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scrub(value, key=None):
    if isinstance(value, dict):
        return {name: scrub(item, name) for name, item in value.items() if name not in HOST_FIELDS}
    if isinstance(value, list):
        return [scrub(item, key) for item in value]
    if isinstance(value, str):
        if key in PATH_FIELDS:
            return Path(value).name
        if key in ("completion", "completion_head", "text_head"):
            return value[:TEXT_HEAD]
        if value.startswith("/"):
            return Path(value).name
    return value


def project_result(source_path):
    raw = source_path.read_bytes()
    content = json.loads(raw)
    projected = scrub(content)
    rows = content.get("per_request") or []
    if rows and isinstance(rows[0], dict) and "completion" in rows[0]:
        for original, row in zip(rows, projected["per_request"]):
            row["completion_sha256"] = digest_bytes(original["completion"].encode("utf-8"))
            row.pop("completion_ids", None)
    return projected, {"bytes": len(raw), "sha256": digest_bytes(raw)}


def read_log(path):
    return path.read_text(encoding="utf-8", errors="replace")


def project_log(text, markers):
    loss_keys = {"loss", "loss_50", "selector_loss_50", "loss_first_window",
                 "loss_last_window", "train_loss", "first_logged_loss", "checks"}
    lines, line_numbers = [], []
    for number, line in enumerate(text.splitlines(), 1):
        candidate = line.strip()
        if candidate.startswith("{"):
            try:
                record = json.loads(candidate)
            except json.JSONDecodeError:
                try:
                    record = ast.literal_eval(candidate)
                except (ValueError, SyntaxError):
                    continue
            if not isinstance(record, dict) or not loss_keys.intersection(record):
                continue
            candidate = json.dumps(scrub(record), ensure_ascii=False)
        elif not (STAGE_LINE.match(candidate) or TARGET_LOSS_LINE.fullmatch(candidate) or candidate.startswith(PHASE_MARKERS)
                  or ARCHITECTURE_LINE.search(candidate) or SPEC_METRICS.search(candidate)):
            continue
        require(find_private(candidate, markers) is None, "PRIVATE_IDENTIFIER_IN_LOG_RECORD")
        lines.append(candidate)
        line_numbers.append(number)
    require(bool(lines), "NO_PUBLISHABLE_LOG_RECORDS")
    return "\n".join(lines) + "\n", line_numbers


def stage_timeline(text):
    stages = []
    for line in text.splitlines():
        match = STAGE_LINE.match(line.strip())
        if match:
            stages.append({"stage": match.group(1), "utc": match.group(2)})
    return stages


def server_activation(text):
    architectures = [{"log_clock": clock, "architecture": name} for clock, name in ARCHITECTURE_LINE.findall(text)]
    spec = SPEC_LINE.search(text)
    metrics = SPEC_METRICS.findall(text)
    # Round 5 launched vLLM with an absolute draft path; keep only the directory name.
    config = re.sub(r"model='([^']*)'", lambda match: f"model='{Path(match.group(1)).name}'", spec.group(1)) if spec else None
    return {
        "resolved_architectures": architectures,
        "speculative_config": config,
        "server_metric_intervals": len(metrics),
        "accepted_tokens_logged": sum(int(item[1]) for item in metrics),
        "drafted_tokens_logged": sum(int(item[2]) for item in metrics),
        "last_logged_mean_acceptance_length": float(metrics[-1][0]) if metrics else None,
    }


def marker_lines(text, markers):
    found = {}
    for line in text.splitlines():
        for marker in markers:
            if line.startswith(marker):
                found[marker] = found.get(marker, 0) + 1
    return found


def training_windows(history_path):
    training = json.loads(history_path.read_text(encoding="utf-8"))
    return training_statistics(training)


ADAPTER_FIELDS = ("sequences", "epochs", "optimizer_steps", "lr", "lora_rank", "lora_alpha", "target_modules",
                  "dropped_long", "max_length", "seed", "first_logged_loss", "last_logged_loss",
                  "wall_clock_seconds", "peak_gpu_gib")
PHASE_MARKERS = ("FINETUNE_TARGET=", "DEGENERATION_GATE=", "TRAIN=", "MEASURE_ACCEPTANCE=", "ANALYSE_PREDICTABILITY=",
                 "VLLM_CLIENT_BENCH=", "MERGED_TARGET_V2", "MERGED_TARGET_ZH", "EXPORT_DRAFTER=", "RESUMED_WITH_QUALITY_WARNING",
                 "BASE_ANALYSIS_REUSED", "ADOPTED_TRAIN_EXIT_CODE=", "===== ROUND4_COMPLETE", "===== ROUND4_EXIT")


def export_round(round_name, source, destination, results, sources, inputs, artifacts, logs_map, log_kind, markers):
    record = {"results": {}, "source": {}, "inputs": {}, "artifacts": {}, "logs": {}}
    for public, private in results.items():
        projected, identity = project_result(source / private)
        target = destination / "results" / round_name / public
        target.parent.mkdir(parents=True, exist_ok=True)
        dump_json(target, projected)
        record["results"][public] = dict(identity, source=private,
                         published_sha256=digest_file(target), published_bytes=target.stat().st_size)
    for public, private in sources.items():
        raw = (source / private).read_bytes()
        text = raw.decode("utf-8")
        require(find_private(text, markers) is None, "PRIVATE_IDENTIFIER_IN_SOURCE:" + public)
        target = destination / "source" / round_name / public
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw.replace(b"\r\n", b"\n"))
        record["source"][public] = {"bytes": len(raw), "sha256": digest_bytes(raw), "source": private}
    for public, private in inputs.items():
        raw = (source / private).read_bytes()
        text = raw.decode("utf-8")
        if public.endswith(".json"):
            text = json.dumps(scrub(json.loads(text)), ensure_ascii=False, indent=2) + "\n"
            raw = text.encode("utf-8")
        require(find_private(text, markers) is None, "PRIVATE_IDENTIFIER_IN_INPUT:" + public)
        target = destination / "inputs" / round_name / public
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        record["inputs"][public] = {"bytes": (source / private).stat().st_size, "sha256": digest_file(source / private),
                                    "published": True, "source": private}
    for public, private in artifacts.items():
        path = source / private
        record["artifacts"][public] = {"bytes": path.stat().st_size, "sha256": digest_file(path),
                                       "published": False, "source": private}
    for name, private in logs_map.items():
        text = read_log(source / private)
        entry = {"bytes": len(text.encode("utf-8")), "sha256": digest_file(source / private), "source": private}
        if log_kind(name) == "pipeline":
            entry["stages"] = stage_timeline(text)
            entry["markers"] = marker_lines(text, PHASE_MARKERS)
        else:
            entry["activation"] = server_activation(text)
        projected, line_numbers = project_log(text, markers)
        target = destination / "logs" / round_name / (name + ".log")
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = projected.encode("utf-8")
        target.write_bytes(raw)
        entry.update(published=True, published_sha256=digest_bytes(raw),
                     published_bytes=len(raw), source_line_numbers=line_numbers,
                     projection="Training metric records, stage/terminal markers and server counters; "
                                "JSON formatting and path fields normalized, other lines retained only in the private raw log.")
        record["logs"][name] = entry
    return record


def export_training(source, round4_source, destination):
    provenance_path = destination / "evidence/provenance.json"
    require(provenance_path.is_file(), "FULL_EXPORT_REQUIRED_FIRST")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    markers = private_markers()
    for round_name, archive, results, sources, logs, log_kind in (
        ("round3", source, ROUND3_RESULT_FILES, SOURCE_FILES, LOG_FILES,
         lambda name: "pipeline" if name.startswith("phase") else "server"),
        ("round4", round4_source, ROUND4_RESULT_FILES, ROUND4_SOURCE_FILES, ROUND4_LOG_FILES,
         lambda name: "pipeline" if name.startswith("round4") else "server"),
    ):
        previous = provenance[round_name]
        for name, entry in previous["source"].items():
            if entry.get("published", True):
                require(digest_file(archive / entry["source"]) == entry["sha256"],
                        "ARCHIVED_SOURCE_CHANGED:" + name)
        for name, entry in previous["logs"].items():
            require(digest_file(archive / entry["source"]) == entry["sha256"],
                    "ARCHIVED_LOG_CHANGED:" + name)
        histories = {name: path for name, path in results.items() if name.startswith("training/")}
        exported = export_round(round_name, archive, destination, histories, sources, {}, {}, logs, log_kind, markers)
        for collection in ("results", "source", "logs"):
            previous[collection].update(exported[collection])
    dump_json(provenance_path, provenance)
    print("TRAINING_EXPORT=PASS histories=4 logs=" + str(len(LOG_FILES) + len(ROUND4_LOG_FILES)))


def export_round5(round5_source, destination):
    """Append the Round 5 serving re-test to an existing export without touching Rounds 3-4."""
    provenance_path = destination / "evidence/provenance.json"
    require(provenance_path.is_file(), "FULL_EXPORT_REQUIRED_FIRST")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    markers = private_markers()
    round5 = export_round("round5", round5_source.resolve(), destination.resolve(), ROUND5_RESULT_FILES, {}, {}, {},
                          ROUND5_LOG_FILES, lambda name: "server", markers)
    # The client that produced these files is byte-identical to the Round 4 snapshot already
    # published under source/round4/; it is recorded by hash rather than duplicated.
    client = provenance["round4"]["source"]["vllm_client_bench.py"]
    round5["source"] = {
        "vllm_client_bench.py": {"published": False, "same_as": "source/round4/vllm_client_bench.py",
                                 "sha256": client["sha256"], "bytes": client["bytes"]},
        "round5.sh": {"published": False,
                      "reason": "orchestration shell with private host paths; the engine flags are identical to round4.sh and transcribed in the README"},
    }
    provenance["round5"] = round5
    dump_json(provenance_path, provenance)
    for path in sorted((destination / "results" / "round5").rglob("*.json")):
        require(find_private(path.read_text(encoding="utf-8"), markers) is None, "PRIVATE_MARKER_IN_PUBLIC_RESULT:" + path.name)
    print("ROUND5_EXPORT=PASS results=" + str(len(round5["results"])) + " logs=" + str(len(round5["logs"])))


def export_round6(round6_source, destination):
    """Append the Round 6 prompt-block and concurrency sweep to an existing export."""
    provenance_path = destination / "evidence/provenance.json"
    require(provenance_path.is_file(), "FULL_EXPORT_REQUIRED_FIRST")
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    markers = private_markers()
    round6 = export_round("round6", round6_source.resolve(), destination.resolve(), ROUND6_RESULT_FILES, ROUND6_SOURCE_FILES,
                          {}, {}, ROUND6_LOG_FILES, lambda name: "server", markers)
    shell = round6_source / "src/round6.sh"
    round6["source"]["round6.sh"] = {"published": False, "bytes": shell.stat().st_size, "sha256": digest_file(shell),
                                     "reason": "orchestration shell with private host paths; engine flags identical to round4.sh"}
    round6["source"]["vllm_client_bench.py"]["note"] = (
        "Local copy the relay transmitted to the VM at launch; the remote copy was not re-hashed because the VM "
        "was deallocated before export. Every published Round 6 record carries the skip/thinking fields only this version emits.")
    provenance["round6"] = round6
    dump_json(provenance_path, provenance)
    for path in sorted((destination / "results" / "round6").rglob("*.json")):
        require(find_private(path.read_text(encoding="utf-8"), markers) is None, "PRIVATE_MARKER_IN_PUBLIC_RESULT:" + path.name)
    print("ROUND6_EXPORT=PASS results=" + str(len(round6["results"])) + " logs=" + str(len(round6["logs"])))


def export(source, round4_source, destination):
    source = source.resolve()
    round4_source = round4_source.resolve()
    destination = destination.resolve()
    require(source.is_dir() and round4_source.is_dir(), "PRIVATE_ARCHIVE_MISSING")
    markers = private_markers()

    round3 = export_round("round3", source, destination, ROUND3_RESULT_FILES, SOURCE_FILES, ROUND3_INPUT_FILES,
                          ARTIFACT_FILES, LOG_FILES, lambda name: "pipeline" if name.startswith("phase") else "server", markers)
    for public, private in ORCHESTRATION_FILES.items():
        path = source / private
        round3["source"][public] = {"bytes": path.stat().st_size, "sha256": digest_file(path), "published": False,
                                    "source": private, "reason": "orchestration shell with private host paths; commands are transcribed in the README"}
    round3["training"] = {
        "target_adapter": {key: json.loads((source / ARTIFACT_FILES["adapter-v2/training_summary.json"]).read_text(encoding="utf-8"))[key]
                           for key in ADAPTER_FIELDS},
        "drafter_v3": training_windows(source / ARTIFACT_FILES["drafter-v3/training-history.json"]),
    }

    round4 = export_round("round4", round4_source, destination, ROUND4_RESULT_FILES, ROUND4_SOURCE_FILES,
                          ROUND4_INPUT_FILES, ROUND4_ARTIFACT_FILES, ROUND4_LOG_FILES,
                          lambda name: "pipeline" if name.startswith("round4") else "server", markers)
    adapter_zh = json.loads((round4_source / "results/r4_finetune_zh_summary.json").read_text(encoding="utf-8"))
    round4["training"] = {
        "target_adapter_zh": {key: adapter_zh[key] for key in ADAPTER_FIELDS},
        "drafter_zh": training_windows(round4_source / "results/r4_drafter_zh_history.json"),
        "drafter_v3_seed1": training_windows(round4_source / "out/drafter-v3-seed1/training-history.json"),
        "drafter_v3_seed2": training_windows(round4_source / "out/drafter-v3-seed2/training-history.json"),
    }
    round4["source"]["round4.sh"] = {"published": False,
                                     "reason": "orchestration shell with private host paths; commands are transcribed in the README"}

    provenance = {
        "scope": "Allowlisted projection of the author's private archive; hashes trace each public file to its private source.",
        "round3": round3,
        "round4": round4,
    }
    dump_json(destination / "evidence" / "provenance.json", provenance)
    for path in sorted(destination.rglob("*.json")):
        text = path.read_text(encoding="utf-8")
        require(find_private(text, markers) is None, "PRIVATE_MARKER_IN_PUBLIC_RESULT:" + path.name)
    print("EXPORT=DONE", "round3_results=" + str(len(round3["results"])), "round4_results=" + str(len(round4["results"])))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="private archive root (gpu-run-20260908)")
    parser.add_argument("--round4-source", type=Path, help="Round 4 raw workspace copy")
    parser.add_argument("--round5-source", type=Path, help="Round 5 raw workspace copy (gpu-run-R5-20260913/raw-workspace)")
    parser.add_argument("--round6-source", type=Path, help="Round 6 raw workspace copy (gpu-run-R6-20260914/raw-workspace)")
    parser.add_argument("--destination", type=Path, default=ROOT)
    parser.add_argument("--training-only", action="store_true",
                        help="Add training histories, source and readable logs to an existing export without rereading weights")
    parser.add_argument("--round5-only", action="store_true",
                        help="Add the Round 5 serving re-test to an existing export")
    parser.add_argument("--round6-only", action="store_true",
                        help="Add the Round 6 prompt-block and concurrency sweep to an existing export")
    args = parser.parse_args()
    if args.round6_only:
        require(args.round6_source is not None, "ROUND6_SOURCE_REQUIRED")
        export_round6(args.round6_source, args.destination)
        return
    if args.round5_only:
        require(args.round5_source is not None, "ROUND5_SOURCE_REQUIRED")
        export_round5(args.round5_source, args.destination)
        return
    require(args.source is not None and args.round4_source is not None, "SOURCE_AND_ROUND4_SOURCE_REQUIRED")
    if args.training_only:
        export_training(args.source, args.round4_source, args.destination)
    else:
        export(args.source, args.round4_source, args.destination)


if __name__ == "__main__":
    main()
