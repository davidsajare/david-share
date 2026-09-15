"""Regression and mutation tests for the paired router-overhead evidence."""

import copy
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("paired_overhead_report", ROOT / "scripts" / "build_paired_overhead.py")
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


class PairedOverheadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records, cls.provenance = REPORT.load_records()
        REPORT.validate(cls.records, cls.provenance)
        cls.pairs = REPORT.build_pairs(cls.records)

    def reject(self, records=None, provenance=None):
        with self.assertRaises(ValueError):
            REPORT.validate(self.records if records is None else records,
                            self.provenance if provenance is None else provenance)

    def test_every_prompt_has_one_warmup_and_three_measured_pairs(self):
        prompts = {r["question_id"] for r in self.records}
        self.assertEqual(len(prompts), self.provenance["prompts"])
        self.assertEqual(len(self.records), len(prompts) * 4 * 2)
        self.assertEqual(len(self.pairs), len(prompts) * 3)

    def test_each_pair_is_one_router_and_one_direct_request(self):
        for pair in self.pairs:
            self.assertEqual(pair["router_served"], pair["direct_served"])
            self.assertIn("luna", pair["router_served"])

    def test_first_side_alternates_so_ordering_cancels(self):
        first = {p["pair_index"]: p["router_first"] for p in self.pairs}
        self.assertEqual(first, {1: False, 2: True, 3: False})

    def test_identical_conditions_on_both_sides(self):
        conditions = {(r["api"], r["reasoning_effort"], r["max_output_tokens"], r["tools_enabled"])
                      for r in self.records}
        self.assertEqual(conditions, {("chat", None, 8704, False)})

    def test_wrong_region_is_rejected(self):
        changed = copy.deepcopy(self.provenance)
        changed["vm_metadata"]["location"] = "eastus2"
        self.reject(provenance=changed)

    def test_missing_request_is_rejected(self):
        self.reject(records=self.records[:-1])

    def test_unredacted_endpoint_is_rejected(self):
        changed = copy.deepcopy(self.records)
        for r in changed:
            r["endpoint_host"] = "example.openai.azure.com"
        self.reject(records=changed)

    def test_delta_is_router_minus_direct(self):
        pair = self.pairs[0]
        self.assertAlmostEqual(pair["ttft_delta_ms"], pair["router_ttft_ms"] - pair["direct_ttft_ms"], places=1)

    def test_committed_artefacts_match_the_raw_file(self):
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_paired_overhead.py"), "--check"],
                                capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VERIFIED", result.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
