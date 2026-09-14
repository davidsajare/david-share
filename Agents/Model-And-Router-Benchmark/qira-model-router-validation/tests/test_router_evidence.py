"""Regression and mutation tests for the retained Task B evidence."""

import copy
import importlib.util
import json
import statistics
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("router_report", ROOT / "scripts" / "build_router_report.py")
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


class RouterEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        archive = ROOT / "outputs" / f"evidence_router_{REPORT.RUN}.json.xz"
        cls.evidence, cls.records, cls.quality, cls.digest = REPORT.load_evidence(archive)
        cls.dataset = [
            json.loads(line)
            for line in (ROOT / "datasets" / "router_taskb.jsonl").read_text(encoding="utf-8").splitlines()
        ]

    def reject(self, records=None, quality=None, dataset=None):
        with self.assertRaises(ValueError):
            REPORT.validate(
                self.records if records is None else records,
                self.quality if quality is None else quality,
                self.dataset if dataset is None else dataset,
            )

    def test_complete_matrix(self):
        arms, questions = REPORT.validate(self.records, self.quality, self.dataset)
        self.assertEqual((len(arms), len(questions), len(self.records), len(self.quality)), (10, 47, 1880, 470))

    def test_executed_source_hashes(self):
        REPORT.validate_sources(self.evidence["provenance"])
        changed = copy.deepcopy(self.evidence["provenance"])
        changed["source_sha256"]["harness.py"] = "0" * 64
        with self.assertRaises(ValueError):
            REPORT.validate_sources(changed)

    def test_missing_request(self):
        self.reject(records=self.records[:-1])

    def test_duplicate_request(self):
        self.reject(records=self.records[:-1] + [self.records[0]])

    def test_missing_question_even_with_matching_subset(self):
        qid = self.dataset[-1]["id"]
        self.reject(
            records=[r for r in self.records if r["question_id"] != qid],
            quality=[q for q in self.quality if q["question_id"] != qid],
            dataset=self.dataset[:-1],
        )

    def test_invalid_request_fields(self):
        mutations = {
            "region": "eastus",
            "api": "responses",
            "tools_enabled": True,
            "truncated": True,
            "status": "failed",
            "error": "test error",
            "warmup": False,
            "run_id": "wrong-run",
            "reasoning_effort": "max",
            "arm": "wrong-arm",
            "complexity_tag": "wrong-tier",
            "cached_tokens": -1,
            "reasoning_tokens": -1,
            "ttft_ms": -1,
            "response_chars": 0,
            "router_attempts": [],
        }
        router = next(i for i, r in enumerate(self.records) if r["router_mode"] and r["warmup"])
        for key, value in mutations.items():
            with self.subTest(field=key):
                changed = copy.deepcopy(self.records)
                changed[router][key] = value
                self.reject(records=changed)

    def test_output_caps_must_match(self):
        changed = copy.deepcopy(self.records)
        changed[0]["max_output_tokens"] += 1
        self.reject(records=changed)

    def test_direct_model_identity(self):
        changed = copy.deepcopy(self.records)
        direct = next(r for r in changed if r["model_requested"] == "gpt-5.6-sol-dz")
        direct["served_model_family"] = "gpt-5.6-luna"
        self.reject(records=changed)

    def test_routing_trace_must_agree_with_served_model(self):
        changed = copy.deepcopy(self.records)
        routed = next(r for r in changed if r["router_mode"])
        other = "gpt-5.6-sol" if routed["served_model_family"] == "gpt-5.6-luna" else "gpt-5.6-luna"
        routed["router_attempts"] = [{"model": other, "status": 200, "error": None}]
        self.reject(records=changed)

    def test_zero_usage_record_is_rejected(self):
        changed = copy.deepcopy(self.records)
        for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "reasoning_tokens"):
            changed[0][key] = 0
        self.reject(records=changed)

    def test_quality_coverage(self):
        self.reject(quality=self.quality[:-1])
        self.reject(quality=self.quality[:-1] + [self.quality[0]])

    def test_quality_score_must_be_valid_integer(self):
        for value in (0, 6, True, 4.5, None):
            with self.subTest(value=value):
                changed = copy.deepcopy(self.quality)
                changed[0][REPORT.analyze.QUALITY_DIMS[0]] = value
                self.reject(quality=changed)

    def test_quality_mean_and_iteration_binding(self):
        for field, value in (("quality_mean", 0), ("iteration", 1), ("run_id", "wrong-run")):
            with self.subTest(field=field):
                changed = copy.deepcopy(self.quality)
                changed[0][field] = value
                self.reject(quality=changed)

    def test_archive_checksum(self):
        source = ROOT / "outputs" / f"evidence_router_{REPORT.RUN}.json.xz"
        with tempfile.TemporaryDirectory() as tmp:
            changed = Path(tmp) / source.name
            payload = bytearray(source.read_bytes())
            payload[-1] ^= 1
            changed.write_bytes(payload)
            with self.assertRaisesRegex(ValueError, "pinned checksum"):
                REPORT.load_evidence(changed)

    def test_cached_and_reasoning_tokens_not_double_billed(self):
        pricing = {"test-model": {"input": 5, "cached": 0.5, "output": 30}}
        cost = REPORT.analyze.cost_for(pricing, "test-model", 1000, 200, 300)
        self.assertAlmostEqual(cost, (800 * 5 + 200 * 0.5 + 300 * 30) / 1_000_000)
        self.assertIsNone(REPORT.analyze.cost_for(pricing, "missing-model", 1000, 200, 300))

    def test_quality_means_recomputed(self):
        for q in self.quality:
            self.assertAlmostEqual(q["quality_mean"], statistics.mean(q[d] for d in REPORT.analyze.QUALITY_DIMS))


if __name__ == "__main__":
    unittest.main()
