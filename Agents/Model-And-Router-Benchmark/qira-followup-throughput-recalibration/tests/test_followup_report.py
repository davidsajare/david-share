"""Follow-up builder: end-to-end run on the retained evidence plus fail-closed mutations."""

import copy
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("followup", ROOT / "scripts" / "build_followup_report.py")
FU = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(FU)


@unittest.skipUnless((FU.RAW).exists() and any(FU.RAW.glob("direct_*.jsonl")), "follow-up evidence not retrieved yet")
class FollowupBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = FU.analyze.load_registry(ROOT / "config" / "models.json")
        cls.pricing = FU.analyze.load_pricing(ROOT / "config" / "pricing.json")
        cls.run_direct = FU.find_run("direct")
        cls.run_load = FU.find_run("loadtest")
        cls.direct = FU.load_jsonl(FU.RAW / f"direct_{cls.run_direct}.jsonl")
        cls.load = FU.load_jsonl(FU.RAW / f"loadtest_{cls.run_load}.jsonl")

    def with_records(self, name, rows, fn):
        real = FU.load_jsonl

        def fake(path):
            return copy.deepcopy(rows) if path.name.startswith(name) else real(path)
        with patch.object(FU, "load_jsonl", fake):
            return fn()

    def test_input_manifest_matches_every_committed_input(self):
        FU.validate_input_manifest()

    def test_input_manifest_rejects_a_modified_public_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = json.loads(
                (FU.INPUTS / "manifest.json").read_text(encoding="utf-8"))
            (root / "manifest.json").write_text(
                json.dumps(manifest), encoding="utf-8")
            for name in manifest["files"]:
                (root / name).write_bytes((FU.INPUTS / name).read_bytes())
            target = root / "quality_v1_20260909_120534.jsonl"
            target.write_bytes(target.read_bytes() + b" ")
            with patch.object(FU, "INPUTS", root), \
                    self.assertRaisesRegex(ValueError, "SHA256"):
                FU.validate_input_manifest()

    def test_direct_full_matrix_and_all_valid(self):
        run, records, rows = FU.direct_comparison(self.pricing, self.registry)
        self.assertEqual(len(records), 752)
        self.assertEqual(len(rows), 8)
        self.assertEqual({r["n"] for r in rows}, {141})
        self.assertTrue(all(r["api"] == "responses" for r in records))

    def test_direct_rejects_missing_or_invalid_rows(self):
        for mutate in (lambda rows: rows[:-1],
                       lambda rows: rows[:-1] + [dict(rows[0])],
                       lambda rows: [{**rows[0], "prompt_tokens": 0}] + rows[1:],
                       lambda rows: [{**rows[0], "truncated": True, "status": "incomplete"}] + rows[1:],
                       lambda rows: [{**rows[0], "region": "eastus"}] + rows[1:],
                       lambda rows: [{**rows[0], "tools_enabled": True}] + rows[1:]):
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.with_records("direct", mutate(list(self.direct)), lambda: FU.direct_comparison(self.pricing, self.registry))

    def test_loadtest_matrix_and_summary_agreement(self):
        run, records, rows = FU.load_test()
        self.assertEqual(len(rows), len(FU.LOAD_ARMS) * len(FU.LEVELS))
        self.assertEqual({r["requests"] for r in rows}, {FU.REQUESTS_PER_LEVEL})
        for r in rows:
            self.assertEqual(r["ok"] + r["errors"], r["requests"])
        with self.assertRaises(ValueError):
            self.with_records("loadtest", list(self.load)[:-1], FU.load_test)
        dropped = [{**r, "error": "RateLimitError: 429", "status": None, "error_class": "429"} if not r["warmup"] and r["level"] == 1 and r["slot"] == 0 else r for r in self.load]
        with self.assertRaises(ValueError):  # summary.json no longer agrees with the records
            self.with_records("loadtest", dropped, FU.load_test)

    def test_rubric_comparison_requires_same_iteration_and_version(self):
        arms, questions, per_task = FU.rubric_comparison()
        self.assertEqual(len(questions), 470 + 187)
        self.assertEqual({r["task"] for r in arms}, {"A", "B"})
        v2 = FU.load_jsonl(FU.OUTPUT / f"quality_v2_{FU.RUN_B}.jsonl")
        for mutate in (lambda rows: rows[:-1],
                       lambda rows: [{**rows[0], "iteration": 99}] + rows[1:],
                       lambda rows: [{**rows[0], "rubric_version": "v1"}] + rows[1:],
                       lambda rows: [{**rows[0], "accuracy": 6}] + rows[1:]):
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.with_records(f"quality_v2_{FU.RUN_B}", mutate(list(v2)), FU.rubric_comparison)

    def test_readmes_are_generated_and_stable(self):
        import subprocess
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_followup_report.py"), "--check"],
                                capture_output=True, text=True, cwd=ROOT, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VERIFIED:", result.stdout)
        for name in ("README.md", "README-CN.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn(self.run_direct, text)
            self.assertIn(self.run_load, text)
            self.assertNotIn("cognitiveservices.azure.com", text)


if __name__ == "__main__":
    unittest.main()
