"""Reject drift in the drafter-adaptation evidence and its README table."""

import copy
import json
from pathlib import Path
import shutil
import tempfile
import unittest

import analyze_results
import export_evidence
import validate_report


ROOT = Path(__file__).resolve().parent
TOPIC = ROOT.parent.parent


class AdaptationEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "topic" / "experiments" / ROOT.name
        self.topic = self.root.parent.parent
        shutil.copytree(ROOT, self.root, ignore=shutil.ignore_patterns("__pycache__"))
        for filename in validate_report.READMES:
            shutil.copyfile(TOPIC / filename, self.topic / filename)

    def mutate_json(self, relative, mutate):
        path = self.root / relative
        content = json.loads(path.read_text(encoding="utf-8"))
        mutate(content)
        path.write_text(json.dumps(content, ensure_ascii=False), encoding="utf-8")

    def test_recorded_evidence_passes(self):
        validate_report.validate(self.root)

    def test_summary_is_recomputed_not_copied(self):
        published = json.loads((self.root / "data/summary.json").read_text(encoding="utf-8"))
        self.assertEqual(published, analyze_results.summarize(self.root))

    def test_paired_bootstrap_refuses_different_target_text(self):
        record = self.root / "results/round4/agreement/ftzh_ours.json"
        content = json.loads(record.read_text(encoding="utf-8"))
        content["per_request"][0]["completion_ids_sha256"] = "0" * 64
        reference = json.loads((self.root / "results/round4/agreement/ftzh_released.json").read_text(encoding="utf-8"))
        result = analyze_results.paired_agreement(reference, content)
        self.assertEqual(result["status"], "NOT_PAIRED")
        self.assertEqual(result["prompts_with_identical_text"], 199)

    def test_round3_marginals_are_not_multiplied_into_a_joint_length(self):
        summary = json.loads((self.root / "data/summary.json").read_text(encoding="utf-8"))
        for entry in summary["round3"]["agreement"].values():
            self.assertIsNone(entry["joint_prefix_acceptance_length"])
        for entry in summary["round3"]["agreement_paired"].values():
            self.assertEqual(entry["status"], "NOT_PAIRED")

    def test_round4_joint_prefix_matches_recorded_value(self):
        record = json.loads((self.root / "results/round4/agreement/ftzh_ours.json").read_text(encoding="utf-8"))
        runs = [value for row in record["per_request"] for value in row["prefix_lengths"]]
        self.assertAlmostEqual(1.0 + sum(runs) / len(runs), record["teacher_forced_acceptance_length"], places=9)

    def test_changed_hit_count_is_rejected(self):
        self.mutate_json("results/round4/agreement/ftzh_ours.json", lambda value: value["per_request"][0]["hits"].__setitem__(0, 0))
        with self.assertRaisesRegex(ValueError, "AGREEMENT_MARGINAL_MISMATCH"):
            analyze_results.summarize(self.root)

    def test_changed_vllm_tokens_are_rejected(self):
        self.mutate_json("results/round4/vllm/dflash_ours.json", lambda value: value["levels"][0].update(completion_tokens=1))
        with self.assertRaisesRegex(ValueError, "VLLM_TOKEN_MISMATCH"):
            analyze_results.summarize(self.root)

    def test_changed_acceptance_length_is_rejected(self):
        self.mutate_json("results/round3/acceptance/v3_selector.json", lambda value: value.update(macro_mean_acceptance_length=9.0))
        with self.assertRaisesRegex(ValueError, "ACCEPTANCE_MACRO_MISMATCH"):
            analyze_results.summarize(self.root)

    def test_gate_verdict_must_match_rows(self):
        self.mutate_json("results/round4/gates/target_zh.json", lambda value: value["verdict"].update(loop_prompts=0))
        with self.assertRaisesRegex(ValueError, "GATE_LOOP_MISMATCH"):
            analyze_results.summarize(self.root)

    def test_readme_table_drift_is_rejected(self):
        for filename in validate_report.READMES:
            with self.subTest(filename=filename):
                path = self.topic / filename
                original = path.read_text(encoding="utf-8")
                self.assertIn("97.1 / 106.0", original)
                path.write_text(original.replace("97.1 / 106.0", "97.1 / 116.0", 1), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "REPORT_DATA_DRIFT:ADAPTATION_TABLE"):
                    validate_report.validate(self.root)
                path.write_text(original, encoding="utf-8")

    def test_private_marker_in_public_file_is_rejected(self):
        path = self.root / "results/round4/vllm/baseline.json"
        content = json.loads(path.read_text(encoding="utf-8"))
        for leaked in ("served from 10.0.0.4", "cache at /home/operator/run", "sub 12345678-1234-1234-1234-123456789abc"):
            with self.subTest(leaked=leaked):
                content["note"] = leaked
                path.write_text(json.dumps(content), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "PRIVATE_MARKER_IN_PUBLIC_FILE"):
                    validate_report.verify_provenance(self.root)

    def test_edited_source_snapshot_is_rejected(self):
        source = self.root / "source/round4/train_drafter.py"
        source.write_bytes(source.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "PUBLISHED_FILE_HASH_OR_SET_MISMATCH"):
            validate_report.verify_manifest(self.root)

    def test_tampered_published_prompts_are_rejected(self):
        prompts = self.root / "inputs/round4/eval_prompts_zh200.jsonl"
        prompts.write_bytes(prompts.read_bytes() + b"\n")
        with self.assertRaisesRegex(ValueError, "INPUT_HASH_MISMATCH:eval_prompts_zh200.jsonl"):
            analyze_results.summarize(self.root)

    def test_tampered_training_history_is_rejected(self):
        self.mutate_json("results/round4/training/drafter_zh_history.json",
                         lambda value: value["history"].__setitem__(0, 999.0))
        with self.assertRaisesRegex(ValueError, "TRAINING_HISTORY_HASH_MISMATCH"):
            analyze_results.training_summary(self.root, validate_report.read_json(self.root / "evidence/provenance.json"))

    def test_tampered_public_log_is_rejected(self):
        path = self.root / "logs/round4/round4.log"
        path.write_bytes(path.read_bytes() + b"TRAIN=PASS\n")
        with self.assertRaisesRegex(ValueError, "PUBLISHED_LOG_HASH_MISMATCH"):
            validate_report.verify_provenance(self.root)

    def test_modified_loss_excerpt_is_rejected(self):
        path = self.topic / "README.md"
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("hidden.detach(), logits.detach()", "hidden, logits", 1), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "LOSS_SOURCE_EXCERPT_DRIFT"):
            validate_report.validate(self.root)

    def test_server_acceptance_is_derived_from_logged_totals(self):
        summary = json.loads((self.root / "data/summary.json").read_text(encoding="utf-8"))
        for round_name in ("round3", "round4", "round5"):
            for route, entry in summary[round_name]["vllm_server_acceptance"].items():
                with self.subTest(round=round_name, route=route):
                    self.assertEqual(entry["drafted_tokens"], entry["verification_steps"] * 7)
                    self.assertAlmostEqual(entry["derived_mean_acceptance_length"],
                                           1.0 + entry["accepted_tokens"] / entry["verification_steps"], places=4)

    def test_round5_cells_hold_four_observations_and_raw_ranges(self):
        summary = json.loads((self.root / "data/summary.json").read_text(encoding="utf-8"))
        routes = summary["round5"]["vllm"]["routes"]
        for route, entry in routes.items():
            for concurrency, cell in entry["levels"].items():
                with self.subTest(route=route, concurrency=concurrency):
                    values = list(cell["observations"].values())
                    self.assertEqual(cell["n"], 4)
                    self.assertEqual(len(values), 4)
                    self.assertEqual(cell["min"], min(values))
                    self.assertEqual(cell["max"], max(values))
                    self.assertAlmostEqual(cell["mean"], sum(values) / 4, places=2)
        comparison = summary["round5"]["vllm"]["adapted_vs_released"]
        for concurrency, entry in comparison.items():
            with self.subTest(concurrency=concurrency):
                released, adapted = routes["dflash_released"]["levels"][concurrency], routes["dflash_ours"]["levels"][concurrency]
                self.assertEqual(entry["released_range"], [released["min"], released["max"]])
                self.assertEqual(entry["adapted_range"], [adapted["min"], adapted["max"]])
                self.assertEqual(entry["ranges_overlap"], not (adapted["min"] > released["max"] or released["min"] > adapted["max"]))

    def test_round5_changed_observation_is_rejected(self):
        self.mutate_json("results/round5/vllm/dflash_ours_repAp1.json",
                         lambda value: value["levels"][0].update(tokens_per_second=value["levels"][0]["tokens_per_second"] + 5.0))
        with self.assertRaisesRegex(ValueError, "VLLM_THROUGHPUT_MISMATCH:dflash_ours:repAp1"):
            analyze_results.summarize(self.root)

    def test_round5_prompts_must_match_round4_chinese_set(self):
        self.mutate_json("results/round5/vllm/discovery.json", lambda value: value.update(prompts_sha256="0" * 64))
        with self.assertRaisesRegex(ValueError, "ROUND5_PROMPTS_DIFFER_FROM_ROUND4"):
            analyze_results.summarize(self.root)

    def test_round5_text_identity_is_recomputed_from_hashes(self):
        summary = json.loads((self.root / "data/summary.json").read_text(encoding="utf-8"))
        identity = summary["round5"]["text_identity"]["1"]
        released = json.loads((self.root / "results/round5/vllm/dflash_released_repAp1.json").read_text(encoding="utf-8"))
        adapted = json.loads((self.root / "results/round5/vllm/dflash_ours_repAp1.json").read_text(encoding="utf-8"))
        level = lambda record: next(item for item in record["levels"] if item["concurrency"] == 1)["per_request"]
        expected = sum(a["text_sha256"] == b["text_sha256"] for a, b in zip(level(released), level(adapted)))
        self.assertEqual(identity["released_vs_adapted"]["repAp1"], expected)
        self.assertEqual(identity["prompts"], 40)

    def test_round5_readme_section_drift_is_rejected(self):
        for filename in validate_report.READMES:
            with self.subTest(filename=filename):
                path = self.topic / filename
                original = path.read_text(encoding="utf-8")
                marker = "#### 设置 B 服务复测（Round 5）" if validate_report.READMES[filename] else "#### Setting B Serving Re-test (Round 5)"
                self.assertIn(marker, original)
                path.write_text(original.replace(marker, marker + " edited", 1), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "REPORT_DATA_DRIFT:ADAPTATION_TABLE"):
                    validate_report.validate(self.root)
                path.write_text(original, encoding="utf-8")

    def test_forged_rule_record_is_rejected(self):
        self.mutate_json(validate_report.RULES, lambda value: value["checks"].append({"id": "extra", "status": "PASS", "evidence": []}))
        with self.assertRaisesRegex(ValueError, "VALIDATION_RECORD_DRIFT"):
            validate_report.validate(self.root)

    def test_replay_entry_required_in_both_readmes(self):
        for filename in validate_report.READMES:
            with self.subTest(filename=filename):
                path = self.topic / filename
                original = path.read_text(encoding="utf-8")
                path.write_text(original.replace(f"python experiments/{ROOT.name}/validate_report.py", "omitted", 1), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "REPLAY_ENTRY_MISSING"):
                    validate_report.validate(self.root)
                path.write_text(original, encoding="utf-8")

    def test_bootstrap_is_deterministic(self):
        reference = json.loads((self.root / "results/round4/agreement/ftzh_released.json").read_text(encoding="utf-8"))
        candidate = json.loads((self.root / "results/round4/agreement/ftzh_ours.json").read_text(encoding="utf-8"))
        first = analyze_results.paired_agreement(reference, candidate)
        second = analyze_results.paired_agreement(copy.deepcopy(reference), copy.deepcopy(candidate))
        self.assertEqual(first, second)
        self.assertEqual(first["status"], "PAIRED")
        self.assertEqual(first["first_offset_hit_rate"]["interpretation"], "positive")


class TrainingExportTests(unittest.TestCase):
    def test_training_windows_are_recomputed(self):
        training = {"history": list(range(20)), "selector_history": [0.5] * 20, "args": {"gamma": "7.0"}}
        measured = analyze_results.training_statistics(training)
        self.assertEqual(measured["window_steps"], 2)
        self.assertEqual(measured["backbone_loss_first_window"], 0.5)
        self.assertEqual(measured["backbone_loss_last_window"], 18.5)
        self.assertEqual(measured["selector_loss_last_window"], 0.5)
        training["selector_history"].pop()
        with self.assertRaisesRegex(ValueError, "TRAINING_HISTORY_LENGTH_MISMATCH"):
            analyze_results.training_statistics(training)

    def test_target_loss_text_is_preserved(self):
        text = "step 10/120 loss 2.5000 lr 1.00e-04 gpu 60.1G elapsed 40s"
        projected, positions = export_evidence.project_log(text, ())
        self.assertEqual(projected, text + "\n")
        self.assertEqual(positions, [1])

    def test_loss_values_and_failed_verdict_survive_log_projection(self):
        text = '\n'.join((
            'private startup at /home/operator/run',
            '{"step": 50, "loss_50": 3.4566, "selector_loss_50": 1.4103, "lr": 0.00004}',
            "{'loss': 0.75, 'epoch': 1.0}",
            '{"checks": {"checkpoint_reloads": false}, "adapter": "/home/operator/model"}',
            'TRAIN=FAIL',
        ))
        projected, positions = export_evidence.project_log(text, ())
        self.assertEqual(positions, [2, 3, 4, 5])
        records = projected.splitlines()
        self.assertEqual(json.loads(records[0])["selector_loss_50"], 1.4103)
        self.assertEqual(json.loads(records[1])["loss"], 0.75)
        self.assertFalse(json.loads(records[2])["checks"]["checkpoint_reloads"])
        self.assertEqual(records[-1], "TRAIN=FAIL")
        self.assertNotIn("/home/", projected)

    def test_private_metric_record_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "PRIVATE_IDENTIFIER_IN_LOG_RECORD"):
            export_evidence.project_log('{"loss": 1.0, "note": "private-workload"}', ("private-workload",))


if __name__ == "__main__":
    unittest.main()
