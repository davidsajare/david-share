"""Recompute the drafter-adaptation summary from the exported result files.

Every number the README shows for this experiment is derived here from the
per-request records, never copied from a log line. Three measurements are kept
apart because they answer different questions:

* agreement   - teacher-forced: both drafters read the same frozen target text and
                are asked, block by block, what the target's next tokens are.
                Round 4 records the leading-correct run per anchor, so a joint-prefix
                acceptance length ``1 + mean(run)`` and a prompt-level paired bootstrap
                are available. Round 3 recorded only per-offset marginal hit rates;
                their product is NOT reported because it assumes independence.
* acceptance  - end-to-end ``dflash_generate``: each drafter actually drafts and the
                target verifies. Different drafters produce different texts, so this
                is a real-use measurement, not a paired one.
* serving     - vLLM client throughput at fixed concurrency, one execution per route.
"""

import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import statistics


ROOT = Path(__file__).resolve().parent
BOOTSTRAP_RESAMPLES = 2000
BOOTSTRAP_SEED = 20260910


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def dump_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def digest_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def bootstrap_interval(identifiers, statistic, resamples=BOOTSTRAP_RESAMPLES, seed=BOOTSTRAP_SEED):
    generator = random.Random(seed)
    differences = sorted(statistic(generator.choices(identifiers, k=len(identifiers))) for _ in range(resamples))
    lower = differences[int(0.025 * (resamples - 1))]
    upper = differences[int(0.975 * (resamples - 1))]
    return [round(lower, 4), round(upper, 4)], ("positive" if lower > 0 else "negative" if upper < 0 else "inconclusive")


def agreement_summary(record):
    rows = record["per_request"]
    require(rows, "AGREEMENT_EMPTY")
    offsets = len(rows[0]["hits"])
    hits = [sum(row["hits"][offset] for row in rows) for offset in range(offsets)]
    trials = [sum(row["trials"][offset] for row in rows) for offset in range(offsets)]
    require(all(trials), "AGREEMENT_OFFSET_WITHOUT_TRIALS")
    for row in rows:
        require(len(set(row["trials"])) == 1 and row["trials"][0] == row["anchors"], "AGREEMENT_TRIAL_ANCHOR_MISMATCH")
    marginal = [hits[offset] / trials[offset] for offset in range(offsets)]
    require(all(abs(a - b) < 1e-9 for a, b in zip(marginal, record["drafter_agreement_by_offset"])),
            "AGREEMENT_MARGINAL_MISMATCH")
    summary = {
        "sequences": len(rows),
        "anchors": sum(row["anchors"] for row in rows),
        "marginal_hit_rate_by_offset": [round(value, 4) for value in marginal],
        "first_offset_hit_rate": round(marginal[0], 4),
        "mean_target_top1_prob": round(statistics.mean(row["mean_target_top1_prob"] for row in rows), 4),
        "mean_target_entropy": round(statistics.mean(row["mean_target_entropy"] for row in rows), 4),
    }
    if "prefix_lengths" in rows[0]:
        require(record.get("metric_version") == "joint-prefix-v2", "JOINT_PREFIX_VERSION_MISSING")
        runs = [value for row in rows for value in row["prefix_lengths"]]
        require(len(runs) == summary["anchors"], "PREFIX_RUN_COUNT_MISMATCH")
        joint = 1.0 + sum(runs) / len(runs)
        require(abs(joint - record["teacher_forced_acceptance_length"]) < 1e-9, "JOINT_PREFIX_MISMATCH")
        summary["joint_prefix_acceptance_length"] = round(joint, 4)
        summary["requested_sequences"] = record["requested_sequences"]
        summary["skipped_requests"] = record["skipped_requests"]
    else:
        summary["joint_prefix_acceptance_length"] = None
        summary["joint_prefix_note"] = ("Per-anchor leading-correct runs were not recorded in this round; only marginal "
                                        "per-offset rates are available. The product of marginals is not reported because "
                                        "it assumes independence across offsets.")
    return summary


def paired_agreement(reference, candidate):
    before = {row["id"]: row for row in reference["per_request"]}
    after = {row["id"]: row for row in candidate["per_request"]}
    require(set(before) == set(after) and before, "AGREEMENT_PROMPT_SETS_DIFFER")
    identifiers = sorted(before)
    text_key = "completion_ids_sha256" if "completion_ids_sha256" in before[identifiers[0]] else "completion_sha256"
    same_text = [identifier for identifier in identifiers
                 if before[identifier][text_key] == after[identifier][text_key]
                 and before[identifier]["anchors"] == after[identifier]["anchors"]]
    if len(same_text) != len(identifiers):
        return {
            "status": "NOT_PAIRED",
            "reason": "target text was regenerated per run; greedy decoding under batched bf16 does not reproduce byte-identical completions",
            "prompts": len(identifiers),
            "prompts_with_identical_text": len(same_text),
        }
    if reference.get("cache_sha256"):
        require(reference["cache_sha256"] == candidate.get("cache_sha256"), "AGREEMENT_CACHE_DIFFERS")

    def first_rate(rows):
        return sum(row["hits"][0] for row in rows) / sum(row["trials"][0] for row in rows)

    result = {"status": "PAIRED", "paired_prompts": len(identifiers), "same_target_text": True,
              "bootstrap_unit": "prompt", "bootstrap_resamples": BOOTSTRAP_RESAMPLES}
    metrics = {"first_offset_hit_rate": first_rate}
    if "prefix_lengths" in before[identifiers[0]]:
        def joint(rows):
            runs = [value for row in rows for value in row["prefix_lengths"]]
            return 1.0 + sum(runs) / len(runs)
        metrics["joint_prefix_acceptance_length"] = joint
    for name, statistic in metrics.items():
        reference_value = statistic([before[i] for i in identifiers])
        candidate_value = statistic([after[i] for i in identifiers])
        interval, verdict = bootstrap_interval(
            identifiers, lambda chosen: statistic([after[i] for i in chosen]) - statistic([before[i] for i in chosen]))
        result[name] = {"reference": round(reference_value, 4), "candidate": round(candidate_value, 4),
                        "difference": round(candidate_value - reference_value, 4),
                        "bootstrap_95_percent_interval": interval, "interpretation": verdict}
    return result


def acceptance_summary(record):
    rows = record["per_request"]
    lengths = [value for row in rows for value in row["acceptance_lengths"]]
    require(lengths, "ACCEPTANCE_EMPTY")
    macro = statistics.mean(row["mean_acceptance_length"] for row in rows)
    micro = statistics.mean(lengths)
    require(abs(macro - record["macro_mean_acceptance_length"]) < 1e-9, "ACCEPTANCE_MACRO_MISMATCH")
    require(abs(micro - record["micro_mean_acceptance_length"]) < 1e-9, "ACCEPTANCE_MICRO_MISMATCH")
    require(sum(row["num_output_tokens"] for row in rows) == record["total_output_tokens"], "ACCEPTANCE_TOKEN_MISMATCH")
    return {
        "prompts": len(rows),
        "macro_mean_acceptance_length": round(macro, 4),
        "micro_mean_acceptance_length": round(micro, 4),
        "verification_steps": len(lengths),
        "output_tokens": record["total_output_tokens"],
        "full_block_fraction": round(sum(1 for value in lengths if value >= record["block_size"]) / len(lengths), 4),
        "max_new_tokens": record["max_new_tokens"],
        "capped_prompts": sum(1 for row in rows if row["num_output_tokens"] >= record["max_new_tokens"]),
    }


def text_overlap(left, right):
    left_rows = {row["id"]: row["completion_sha256"] for row in left["per_request"]}
    right_rows = {row["id"]: row["completion_sha256"] for row in right["per_request"]}
    require(set(left_rows) == set(right_rows), "ACCEPTANCE_PROMPT_SETS_DIFFER")
    return {"identical_completions": sum(1 for identifier in left_rows if left_rows[identifier] == right_rows[identifier]),
            "prompts": len(left_rows)}


def hf_summary(record):
    configs = {config["summary"]["label"]: config for config in record["configs"]}
    require(set(configs) == {"autoregressive", "released", "v3"}, "HF_CONFIGS_MISSING")
    reference = {row["index"]: row["text_sha256"] for row in configs["autoregressive"]["per_request"]}
    result = {}
    for label, config in configs.items():
        rows = config["per_request"]
        tokens = sum(row["new_tokens"] for row in rows)
        seconds = sum(row["seconds"] for row in rows)
        require(abs(tokens / seconds - config["summary"]["tokens_per_second"]) < 0.01, "HF_THROUGHPUT_MISMATCH:" + label)
        result[label] = {
            "requests": len(rows),
            "new_tokens": tokens,
            "seconds": round(seconds, 2),
            "tokens_per_second": round(tokens / seconds, 2),
            "identical_text_to_autoregressive": sum(1 for row in rows if reference[row["index"]] == row["text_sha256"]),
        }
    return result


def vllm_summary(records, routes):
    result = {}
    for route in routes:
        record = records[route]
        levels = {}
        for level in record["levels"]:
            rows = level["per_request"]
            tokens = sum(row["completion_tokens"] for row in rows)
            require(tokens == level["completion_tokens"], "VLLM_TOKEN_MISMATCH:" + route)
            # The client rounded wall time to 0.01 s before saving; the recorded rate used the
            # unrounded wall time, so allow the rounding-induced relative error.
            rounding_error = tokens * 0.005 / level["wall_seconds"] ** 2 + 0.005
            require(abs(tokens / level["wall_seconds"] - level["tokens_per_second"]) <= rounding_error,
                    "VLLM_THROUGHPUT_MISMATCH:" + route)
            levels[str(level["concurrency"])] = {
                "requests": level["requests"],
                "completion_tokens": tokens,
                "wall_seconds": level["wall_seconds"],
                "tokens_per_second": level["tokens_per_second"],
                "median_request_seconds": level["median_request_seconds"],
                "length_stops": level["finish_length"],
            }
        result[route] = {"max_tokens": record["max_tokens"], "prompts": record["num_prompts"], "levels": levels}
    baseline = result[routes[0]]["levels"]
    for route in routes[1:]:
        for concurrency, level in result[route]["levels"].items():
            level["speedup_vs_baseline"] = round(level["tokens_per_second"] / baseline[concurrency]["tokens_per_second"], 3)
    return result


def vllm_server_acceptance(provenance_round, routes, speculative_tokens=7):
    """Acceptance length recomputed from the server's own logged totals.

    vLLM logs ``Accepted: N tokens, Drafted: M tokens`` per metrics interval. Each
    verification step drafts ``speculative_tokens`` tokens, so ``M / speculative_tokens``
    is the step count and ``1 + N / steps`` is the mean acceptance length including the
    bonus token, the same definition as ``dflash_generate``. This covers every request
    the server saw during the run, including warmup, so it is a corroborating aggregate
    rather than a per-prompt measurement.
    """
    result = {}
    for route in routes:
        # Round 5 started each route twice; ``logs`` may hold one or several server logs per route.
        keys = [key for key in provenance_round["logs"] if key == "vllm_" + route or key.startswith("vllm_" + route + "_rep")]
        require(bool(keys), "SERVER_LOG_MISSING:" + route)
        accepted = sum(provenance_round["logs"][key]["activation"]["accepted_tokens_logged"] for key in keys)
        drafted = sum(provenance_round["logs"][key]["activation"]["drafted_tokens_logged"] for key in keys)
        require(drafted % speculative_tokens == 0 and drafted > 0, "SERVER_DRAFT_COUNT_NOT_MULTIPLE_OF_BLOCK:" + route)
        steps = drafted // speculative_tokens
        last = provenance_round["logs"][sorted(keys)[-1]]["activation"]
        result[route] = {
            "accepted_tokens": accepted,
            "drafted_tokens": drafted,
            "verification_steps": steps,
            "derived_mean_acceptance_length": round(1.0 + accepted / steps, 4),
            "last_logged_interval_value": last["last_logged_mean_acceptance_length"],
            "logged_intervals": sum(provenance_round["logs"][key]["activation"]["server_metric_intervals"] for key in keys),
            "server_logs": sorted(keys),
        }
    return result


ROUND5_RUNS = tuple(f"rep{rep}p{pas}" for rep in "AB" for pas in (1, 2))


def repeated_vllm_summary(records, routes, runs):
    """Four throughput observations per route and concurrency, kept as observations.

    The runs share weights, prompts, seed and engine flags, so greedy decoding makes the
    token sequences deterministic; the spread below is timing jitter of the same work, not
    sampling variance of the effect. ``ranges_overlap`` compares the raw min/max of the
    adapted and released routes without any distributional assumption.
    """
    per_route = {}
    for route in routes:
        levels = {}
        for run in runs:
            record = records[(route, run)]
            for level in record["levels"]:
                rows = level["per_request"]
                tokens = sum(row["completion_tokens"] for row in rows)
                require(tokens == level["completion_tokens"], f"VLLM_TOKEN_MISMATCH:{route}:{run}")
                rounding_error = tokens * 0.005 / level["wall_seconds"] ** 2 + 0.005
                require(abs(tokens / level["wall_seconds"] - level["tokens_per_second"]) <= rounding_error,
                        f"VLLM_THROUGHPUT_MISMATCH:{route}:{run}")
                cell = levels.setdefault(str(level["concurrency"]), {"observations": {}, "completion_tokens": {}, "length_stops": {}})
                cell["observations"][run] = level["tokens_per_second"]
                cell["completion_tokens"][run] = tokens
                cell["length_stops"][run] = level["finish_length"]
        for cell in levels.values():
            values = list(cell["observations"].values())
            require(len(values) == len(runs), "ROUND5_RUN_MISSING:" + route)
            cell.update(n=len(values), mean=round(statistics.mean(values), 2), min=min(values), max=max(values),
                        stdev=round(statistics.stdev(values), 2))
        per_route[route] = {"max_tokens": records[(route, runs[0])]["max_tokens"],
                            "prompts": records[(route, runs[0])]["num_prompts"], "levels": levels}
    baseline = per_route[routes[0]]["levels"]
    for route in routes[1:]:
        for concurrency, cell in per_route[route]["levels"].items():
            cell["speedup_vs_baseline_mean"] = round(cell["mean"] / baseline[concurrency]["mean"], 3)
    released, adapted = per_route[routes[1]]["levels"], per_route[routes[2]]["levels"]
    comparison = {}
    for concurrency in released:
        r, o = released[concurrency], adapted[concurrency]
        comparison[concurrency] = {
            "adapted_over_released_mean_pct": round((o["mean"] / r["mean"] - 1) * 100, 2),
            "released_range": [r["min"], r["max"]], "adapted_range": [o["min"], o["max"]],
            "ranges_overlap": not (o["min"] > r["max"] or r["min"] > o["max"]),
        }
    return {"routes": per_route, "adapted_vs_released": comparison}


def text_identity(records, routes, runs):
    """How many of the 40 prompts produce byte-identical text, by ``text_sha256``.

    Three comparisons: released vs adapted draft model in the same run (does the drafter
    change the output?), no-speculation vs released in the same run (is greedy speculative
    decoding byte-identical to greedy autoregressive decoding on this engine?), and the
    same route across the two server starts (is the divergence deterministic?).
    """
    def hashes(route, run, concurrency):
        level = next(level for level in records[(route, run)]["levels"] if str(level["concurrency"]) == concurrency)
        return [row["text_sha256"] for row in level["per_request"]]

    concurrencies = [str(level["concurrency"]) for level in records[(routes[0], runs[0])]["levels"]]
    result = {}
    for concurrency in concurrencies:
        entry = {"prompts": len(hashes(routes[0], runs[0], concurrency))}
        entry["released_vs_adapted"] = {run: sum(a == b for a, b in zip(hashes(routes[1], run, concurrency), hashes(routes[2], run, concurrency))) for run in runs}
        entry["baseline_vs_released"] = {run: sum(a == b for a, b in zip(hashes(routes[0], run, concurrency), hashes(routes[1], run, concurrency))) for run in runs}
        entry["same_route_across_server_starts"] = {route: sum(a == b for a, b in zip(hashes(route, runs[0], concurrency), hashes(route, runs[-1], concurrency))) for route in routes}
        result[concurrency] = entry
    return result


def summarize_round5(results, root, provenance, round4_prompts_sha256):
    routes = ("baseline", "dflash_released", "dflash_ours")
    discovery = read_json(results / "vllm" / "discovery.json")
    require(discovery["prompts_sha256"] == round4_prompts_sha256, "ROUND5_PROMPTS_DIFFER_FROM_ROUND4")
    records = {(route, run): read_json(results / "vllm" / f"{route}_{run}.json") for route in routes for run in ROUND5_RUNS}
    for (route, run), record in records.items():
        require(record["label"] == f"{route}_{run}", f"ROUND5_LABEL_MISMATCH:{route}:{run}")
        require(record["num_prompts"] == 40 and record["max_tokens"] == 256, f"ROUND5_CONTRACT_MISMATCH:{route}:{run}")
    return {
        "scope": "Serving re-test of Round 4 setting B (Chinese target, released vs adapted draft model) on 2026-09-13: fresh VM session and vLLM install, same weights, prompts, seed and engine flags; three concurrency levels; four observations per cell.",
        "eval_prompts_sha256": discovery["prompts_sha256"],
        "order": {"repA": "baseline, released, adapted", "repB": "adapted, released, baseline"},
        "vllm": repeated_vllm_summary(records, routes, ROUND5_RUNS),
        "text_identity": text_identity(records, routes, ROUND5_RUNS),
        "vllm_server_acceptance": vllm_server_acceptance(provenance["round5"], routes[1:]),
        "boundary": "Observations are timing repeats of deterministic greedy runs on the same 40 prompts; they bound measurement jitter, not prompt-set sampling variance. No answer grading.",
    }


def verify_inputs(root, round_name, expected):
    """Published prompt files must hash to the prompts_sha256 the run recorded."""
    for filename, recorded in expected.items():
        path = root / "inputs" / round_name / filename
        require(path.is_file(), "PUBLISHED_INPUT_MISSING:" + filename)
        require(digest_file(path) == recorded, "INPUT_HASH_MISMATCH:" + filename)
    return {filename: recorded for filename, recorded in expected.items()}


def gate_summary(record):
    rows = record["per_request"]
    verdict = record["verdict"]
    mean_repeat = statistics.mean(row["repeat_4gram"] for row in rows)
    loops = sum(1 for row in rows if row["max_4gram_count"] >= 3)
    capped = sum(1 for row in rows if row["hit_cap"]) / len(rows)
    require(abs(round(mean_repeat, 4) - verdict["mean_repeat_4gram"]) < 1e-9, "GATE_REPEAT_MISMATCH")
    require(loops == verdict["loop_prompts"], "GATE_LOOP_MISMATCH")
    require(abs(round(capped, 3) - verdict["cap_fraction"]) < 1e-9, "GATE_CAP_MISMATCH")
    return {
        "prompts": len(rows),
        "mean_repeat_4gram": verdict["mean_repeat_4gram"],
        "loop_prompts": loops,
        "cap_fraction": verdict["cap_fraction"],
        "mean_new_tokens": verdict["mean_new_tokens"],
        "mean_top1": verdict["mean_top1"],
        "thresholds": verdict["thresholds"],
        "passed": verdict["pass"],
        "repetition_unit": verdict.get("repetition_unit", "word"),
    }


def summarize_round3(results, root, provenance):
    agreement = {name: read_json(results / "agreement" / f"{name}.json")
                 for name in ("released_selector", "released_argmax", "v3_selector", "v3_argmax")}
    acceptance = {name: read_json(results / "acceptance" / f"{name}.json") for name in agreement}
    prompts_hash = {record["prompts_sha256"] for record in acceptance.values()}
    require(len(prompts_hash) == 1, "ACCEPTANCE_PROMPT_FILE_DIFFERS")
    hf = read_json(results / "hf-throughput.json")
    require(hf["prompts_sha256"] in prompts_hash, "HF_PROMPT_FILE_DIFFERS")
    routes = ("baseline", "dflash_released", "dflash_v3")
    return {
        "scope": "English medical prompts; target = Qwen3.8-27B + LoRA r16 attention-only (1 epoch); drafters = released DFlash 2 vs continuation-trained v3.",
        "eval_prompts_sha256": next(iter(prompts_hash)),
        "published_inputs": verify_inputs(root, "round3", {"eval_prompts_en40.jsonl": next(iter(prompts_hash))}),
        "target_gates": {name: gate_summary(read_json(results / "gates" / f"{name}.json")) for name in ("target_v2", "base_target")},
        "agreement": {name: agreement_summary(record) for name, record in agreement.items()},
        "agreement_paired": {f"v3_minus_released_{path}": paired_agreement(agreement[f"released_{path}"], agreement[f"v3_{path}"])
                             for path in ("selector", "argmax")},
        "acceptance": {name: acceptance_summary(record) for name, record in acceptance.items()},
        "acceptance_text_overlap": {
            "released_selector_vs_v3_selector": text_overlap(acceptance["released_selector"], acceptance["v3_selector"]),
            "released_argmax_vs_v3_argmax": text_overlap(acceptance["released_argmax"], acceptance["v3_argmax"]),
            "released_selector_vs_released_argmax": text_overlap(acceptance["released_selector"], acceptance["released_argmax"]),
        },
        "hf_reference_path": hf_summary(hf),
        "vllm": vllm_summary({route: read_json(results / "vllm" / f"{route}.json") for route in routes}, routes),
        "vllm_server_acceptance": vllm_server_acceptance(provenance["round3"], routes[1:]),
    }


def summarize_round4(results, root, provenance):
    names = ("base_zh_released", "ftzh_released", "ftzh_released_argmax", "ftzh_ours", "ftzh_ours_argmax",
             "en200_released", "en200_v3_seed0", "en200_v3_seed1", "en200_v3_seed2")
    agreement = {name: read_json(results / "agreement" / f"{name}.json") for name in names}
    acceptance = {name: read_json(results / "acceptance" / f"{name}.json") for name in ("ftzh_released", "ftzh_ours")}
    zh_hash = {record["prompts_sha256"] for record in acceptance.values()}
    require(len(zh_hash) == 1, "ACCEPTANCE_PROMPT_FILE_DIFFERS")
    zh_hash = next(iter(zh_hash))
    for name in ("base_zh_released", "ftzh_released", "ftzh_ours"):
        require(agreement[name]["generation_contract"]["prompts_sha256"] == zh_hash, "AGREEMENT_PROMPT_FILE_DIFFERS:" + name)
    en_hash = agreement["en200_released"]["generation_contract"]["prompts_sha256"]
    routes = ("baseline", "dflash_released", "dflash_ours")
    paired = {
        "chinese_ours_minus_released_selector": paired_agreement(agreement["ftzh_released"], agreement["ftzh_ours"]),
        "chinese_ours_minus_released_argmax": paired_agreement(agreement["ftzh_released_argmax"], agreement["ftzh_ours_argmax"]),
    }
    for seed in (0, 1, 2):
        paired[f"english_v3_seed{seed}_minus_released"] = paired_agreement(agreement["en200_released"], agreement[f"en200_v3_seed{seed}"])
    return {
        "scope": "Chinese medical prompts; target = Qwen3.8-27B + LoRA r128 all-modules (2 epochs); drafters = released DFlash 2 vs continuation-trained zh. English 200-prompt re-test of Round 3 with three training seeds.",
        "eval_prompts_sha256": {"zh200": zh_hash, "en200": en_hash},
        "published_inputs": verify_inputs(root, "round4", {"eval_prompts_zh200.jsonl": zh_hash, "eval_prompts_en200.jsonl": en_hash}),
        "target_gates": {name: gate_summary(read_json(results / "gates" / f"{name}.json")) for name in ("target_zh", "base_target_zh")},
        "agreement": {name: agreement_summary(record) for name, record in agreement.items()},
        "agreement_paired": paired,
        "acceptance": {name: acceptance_summary(record) for name, record in acceptance.items()},
        "acceptance_text_overlap": {"ftzh_released_vs_ftzh_ours": text_overlap(acceptance["ftzh_released"], acceptance["ftzh_ours"])},
        "vllm": vllm_summary({route: read_json(results / "vllm" / f"{route}.json") for route in routes}, routes),
        "vllm_server_acceptance": vllm_server_acceptance(provenance["round4"], routes[1:]),
    }


def training_statistics(training):
    history, selector = training["history"], training["selector_history"]
    require(bool(history) and len(history) == len(selector), "TRAINING_HISTORY_LENGTH_MISMATCH")
    require(all(math.isfinite(value) for value in history + selector), "NONFINITE_TRAINING_LOSS")
    window = max(1, len(history) // 10)
    return {
        "args": {key: value for key, value in training["args"].items()
                 if key not in ("target", "adapter", "drafter", "data", "output", "config", "smoke")},
        "steps": len(history),
        "backbone_loss_first_window": round(sum(history[:window]) / window, 4),
        "backbone_loss_last_window": round(sum(history[-window:]) / window, 4),
        "selector_loss_first_window": round(sum(selector[:window]) / window, 4),
        "selector_loss_last_window": round(sum(selector[-window:]) / window, 4),
        "window_steps": window,
    }


def training_summary(root, provenance):
    result = {}
    for name, round_name, training_key, filename in (
        ("english_seed20260908", "round3", "drafter_v3", "drafter_v3_history.json"),
        ("english_seed1", "round4", "drafter_v3_seed1", "drafter_v3_seed1_history.json"),
        ("english_seed2", "round4", "drafter_v3_seed2", "drafter_v3_seed2_history.json"),
        ("chinese_seed20260908", "round4", "drafter_zh", "drafter_zh_history.json"),
    ):
        relative = f"results/{round_name}/training/{filename}"
        path = root / relative
        require(path.is_file(), "TRAINING_HISTORY_MISSING:" + filename)
        identity = provenance[round_name]["results"]["training/" + filename]
        checksum = digest_file(path)
        require(checksum == identity["published_sha256"], "TRAINING_HISTORY_HASH_MISMATCH:" + filename)
        measured = training_statistics(read_json(path))
        require(measured == provenance[round_name]["training"][training_key], "TRAINING_WINDOW_MISMATCH:" + filename)
        result[name] = dict(measured, history_path=relative, history_sha256=checksum)
    return result


def summarize(root):
    results = root / "results"
    provenance = read_json(root / "evidence" / "provenance.json")
    round4 = summarize_round4(results / "round4", root, provenance)
    round5 = summarize_round5(results / "round5", root, provenance, round4["eval_prompts_sha256"]["zh200"])
    return {
        "scope": "Continuation training of the released DFlash 2 drafter against LoRA-fine-tuned Qwen3.8-27B targets. Not training from scratch. Two drift regimes; the Chinese serving comparison was repeated in a later session and then swept across five prompt blocks and four concurrency levels.",
        "round3": summarize_round3(results / "round3", root, provenance),
        "round4": round4,
        "round5": round5,
        "round6": summarize_round6(results / "round6", root, provenance, round4["eval_prompts_sha256"]["zh200"], round5["vllm"]["routes"]),
        "training": training_summary(root, provenance),
        "answer_quality": "NOT_MEASURED: no grader was run on these medical prompt sets; agreement and acceptance describe drafting, not answer correctness.",
        "statistics_boundary": "PAIRED agreement comparisons share byte-identical target text and use a prompt-level bootstrap. Round 3/4 acceptance and vLLM figures are single executions on different generated texts. Round 5 repeats the Round 4 serving runs four times per cell and reports raw ranges. Round 6 serves five disjoint 40-prompt blocks once each and reports the per-block gain; no significance claim.",
    }


ROUND6_BLOCKS = (0, 1, 2, 3, 4)
ROUND6_CONCURRENCIES = ("1", "4", "8", "16")


def block_gain_summary(records, routes, blocks, concurrencies):
    """One serving run per route and prompt block; the gain is recomputed per block, never pooled.

    Five disjoint 40-prompt blocks give five independent estimates of the adapted-over-released
    throughput ratio at each concurrency. ``all_blocks_positive`` is the only aggregate claim the
    README is allowed to make; means and ranges are reported as raw numbers.
    """
    def tps(route, block):
        record = records[(route, block)]
        table = {}
        for level in record["levels"]:
            rows = level["per_request"]
            tokens = sum(row["completion_tokens"] for row in rows)
            require(tokens == level["completion_tokens"], f"VLLM_TOKEN_MISMATCH:{route}:b{block}")
            rounding_error = tokens * 0.005 / level["wall_seconds"] ** 2 + 0.005
            require(abs(tokens / level["wall_seconds"] - level["tokens_per_second"]) <= rounding_error,
                    f"VLLM_THROUGHPUT_MISMATCH:{route}:b{block}")
            table[str(level["concurrency"])] = level["tokens_per_second"]
        require(set(table) == set(concurrencies), f"ROUND6_CONCURRENCY_SET_MISMATCH:{route}:b{block}")
        return table

    throughput = {route: {block: tps(route, block) for block in blocks} for route in routes}
    base, released, adapted = (throughput[route] for route in routes)
    per_concurrency = {}
    for concurrency in concurrencies:
        gains = [round((adapted[b][concurrency] / released[b][concurrency] - 1) * 100, 2) for b in blocks]
        per_concurrency[concurrency] = {
            "adapted_over_released_pct_per_block": gains,
            "gain_mean": round(statistics.mean(gains), 2), "gain_min": min(gains), "gain_max": max(gains),
            "all_blocks_positive": all(g > 0 for g in gains),
            "released_speedup_per_block": [round(released[b][concurrency] / base[b][concurrency], 3) for b in blocks],
            "adapted_speedup_per_block": [round(adapted[b][concurrency] / base[b][concurrency], 3) for b in blocks],
        }
        per_concurrency[concurrency]["released_speedup_mean"] = round(statistics.mean(per_concurrency[concurrency]["released_speedup_per_block"]), 3)
        per_concurrency[concurrency]["adapted_speedup_mean"] = round(statistics.mean(per_concurrency[concurrency]["adapted_speedup_per_block"]), 3)
    return {"tokens_per_second": {route: {f"b{b}": table for b, table in blocks_table.items()} for route, blocks_table in throughput.items()},
            "per_concurrency": per_concurrency}


def block_text_identity(records, routes, blocks, concurrencies):
    def hashes(route, block, concurrency):
        level = next(level for level in records[(route, block)]["levels"] if str(level["concurrency"]) == concurrency)
        return [row["text_sha256"] for row in level["per_request"]]
    result = {}
    for concurrency in concurrencies:
        result[concurrency] = {
            "released_vs_adapted_per_block": [sum(a == b for a, b in zip(hashes(routes[1], b, concurrency), hashes(routes[2], b, concurrency))) for b in blocks],
            "baseline_vs_released_per_block": [sum(a == b for a, b in zip(hashes(routes[0], b, concurrency), hashes(routes[1], b, concurrency))) for b in blocks],
        }
    return result


def thinking_attempt(records, routes):
    """The block-0 thinking pass is published but judged on what the client actually retrieved.

    A valid thinking run must return reasoning text; here every response came back with empty
    ``content`` and zero ``reasoning_chars`` while the server still billed ~130 tokens, so the
    mode the model ran in is unknown and any byte-identity count would compare empty strings.
    """
    per_route = {}
    empty_everywhere = True
    for route in routes:
        record = records[route]
        require(record.get("thinking") is True, "THINKING_FLAG_MISSING:" + route)
        levels = {}
        for level in record["levels"]:
            rows = level["per_request"]
            reasoning = sum(row.get("reasoning_chars", 0) for row in rows)
            content_chars = sum(len(row.get("text_head", "")) for row in rows)
            if reasoning or content_chars:
                empty_everywhere = False
            levels[str(level["concurrency"])] = {
                "tokens_per_second": level["tokens_per_second"], "completion_tokens": level["completion_tokens"],
                "reasoning_chars_total": reasoning, "content_head_chars_total": content_chars,
                "length_stops": level["finish_length"], "median_completion_tokens": statistics.median(row["completion_tokens"] for row in rows),
            }
        per_route[route] = {"max_tokens": record["max_tokens"], "levels": levels}
    return {
        "valid": not empty_everywhere,
        "reason": None if not empty_everywhere else "CLIENT_RETRIEVED_NO_TEXT: every response had empty content and zero reasoning_chars; the model's decoding mode is unverified and throughput/identity numbers are not interpretable",
        "routes": per_route,
    }


def summarize_round6(results, root, provenance, round4_prompts_sha256, round5_routes):
    routes = ("baseline", "dflash_released", "dflash_ours")
    discovery = read_json(results / "vllm" / "discovery.json")
    require(discovery["prompts_sha256"] == round4_prompts_sha256, "ROUND6_PROMPTS_DIFFER_FROM_ROUND4")
    records = {(route, block): read_json(results / "vllm" / f"{route}_b{block}.json") for route in routes for block in ROUND6_BLOCKS}
    for (route, block), record in records.items():
        require(record["label"] == f"{route}_b{block}", f"ROUND6_LABEL_MISMATCH:{route}:b{block}")
        require(record["skip"] == block * 40 and record["num_prompts"] == 40 and record["max_tokens"] == 256 and record["thinking"] is False,
                f"ROUND6_CONTRACT_MISMATCH:{route}:b{block}")
    thinking = {route: read_json(results / "vllm" / f"{route}_think_b0.json") for route in routes}
    gains = block_gain_summary(records, routes, ROUND6_BLOCKS, ROUND6_CONCURRENCIES)
    # Block 0 is the Round 4/5 prompt set; its deviation from the Round 5 four-run means is the regression anchor.
    anchor = {}
    for route in routes:
        anchor[route] = {c: round((gains["tokens_per_second"][route]["b0"][c] / round5_routes[route]["levels"][c]["mean"] - 1) * 100, 2)
                         for c in ("1", "4", "8")}
    return {
        "scope": "Setting B target and both draft models served once per route on 2026-09-14 across five disjoint 40-prompt blocks of the same 200 held-out Chinese prompts (block 0 = Round 4/5 set) at concurrency 1/4/8/16; plus one block-0 thinking-mode attempt.",
        "eval_prompts_sha256": discovery["prompts_sha256"],
        "blocks": {f"b{b}": {"skip": b * 40, "prompts": 40} for b in ROUND6_BLOCKS},
        "vllm": gains,
        "text_identity": block_text_identity(records, routes, ROUND6_BLOCKS, ROUND6_CONCURRENCIES),
        "block0_vs_round5_mean_pct": anchor,
        "thinking_attempt": thinking_attempt(thinking, routes),
        "vllm_server_acceptance": vllm_server_acceptance(provenance["round6"], routes[1:]),
        "boundary": "One run per block; the five blocks are independent prompt samples but a single timing observation each. Concurrency 16 equals the engine's max-num-seqs. No answer grading.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()
    summary = summarize(args.root)
    output = args.output or (args.root / "data" / "summary.json")
    dump_json(output, summary)
    print("SUMMARY_WRITTEN", output)


if __name__ == "__main__":
    main()
