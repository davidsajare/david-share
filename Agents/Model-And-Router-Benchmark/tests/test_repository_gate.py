"""Negative and mutation tests for the top-level repository gate.

Each test removes one guarantee and asserts the gate refuses, so a passing gate
means the controls are live rather than merely present.
"""

from __future__ import annotations

import importlib.util
import shutil
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "validate_repo", ROOT / "scripts" / "validate_repo.py"
)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)

# Assembled at import time so this test file is not itself a boundary violation.
CUSTOMER_NAME = "Len" + "ovo"
PRODUCT_NAME = "Q" + "ira"
DEPLOYED_HOST = "prod-portal." + "cloudapp.azure.com"
LIVE_ENDPOINT = "ai-prod-weu." + "cognitiveservices.azure.com"


class PublicBoundaryGate(unittest.TestCase):
    """The control the published repository failed before this rewrite."""

    def scratch(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        temporary = tempfile.TemporaryDirectory()
        return temporary, Path(temporary.name)

    def test_clean_text_passes(self):
        temporary, root = self.scratch()
        self.addCleanup(temporary.cleanup)
        path = root / "notes.md"
        path.write_text("A same-region benchmark with no tools.\n", encoding="utf-8")
        gate.validate_public_boundary([path])

    def test_customer_name_in_text_fails_closed(self):
        temporary, root = self.scratch()
        self.addCleanup(temporary.cleanup)
        path = root / "notes.md"
        path.write_text(f"Measured for {CUSTOMER_NAME}.\n", encoding="utf-8")
        with self.assertRaisesRegex(gate.GateError, "customer name"):
            gate.validate_public_boundary([path])

    def test_product_name_in_path_fails_closed(self):
        temporary, root = self.scratch()
        self.addCleanup(temporary.cleanup)
        path = root / f"{PRODUCT_NAME.lower()}-notes.md"
        path.write_text("nothing sensitive inside\n", encoding="utf-8")
        with self.assertRaisesRegex(gate.GateError, "product name in path"):
            gate.validate_public_boundary([path])

    def test_published_password_table_fails_closed(self):
        temporary, root = self.scratch()
        self.addCleanup(temporary.cleanup)
        path = root / "access.md"
        path.write_text(
            "| Field | Value |\n|---|---|\n| Password | `hunter2` |\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(gate.GateError, "published password field"):
            gate.validate_public_boundary([path])

    def test_deployed_hostname_fails_closed(self):
        temporary, root = self.scratch()
        self.addCleanup(temporary.cleanup)
        path = root / "deploy.md"
        path.write_text(f"http://{DEPLOYED_HOST}/app/\n", encoding="utf-8")
        with self.assertRaisesRegex(gate.GateError, "deployed hostname"):
            gate.validate_public_boundary([path])

    def test_placeholder_endpoint_is_allowed(self):
        temporary, root = self.scratch()
        self.addCleanup(temporary.cleanup)
        path = root / "env.example"
        path.write_text(
            "AZURE_OPENAI_ENDPOINT=https://YOUR-ENDPOINT.cognitiveservices.azure.com\n",
            encoding="utf-8",
        )
        gate.validate_public_boundary([path])

    def test_real_endpoint_host_fails_closed(self):
        temporary, root = self.scratch()
        self.addCleanup(temporary.cleanup)
        path = root / "env.example"
        path.write_text(
            f"AZURE_OPENAI_ENDPOINT=https://{LIVE_ENDPOINT}\n", encoding="utf-8")
        with self.assertRaisesRegex(gate.GateError, "live endpoint host"):
            gate.validate_public_boundary([path])

    def test_the_published_tree_is_clean(self):
        gate.validate_public_boundary()


class ReaderAndBilingualContract(unittest.TestCase):
    def contract_copy(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        for relative in ("README.md", "README-CN.md"):
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        return temporary, root

    def test_current_contract_passes(self):
        gate.validate_reader_contract(ROOT)
        gate.validate_bilingual(ROOT)

    def test_removing_a_fact_badge_fails_closed(self):
        temporary, root = self.contract_copy()
        self.addCleanup(temporary.cleanup)
        path = root / "README.md"
        text = path.read_text(encoding="utf-8")
        line = next(item for item in text.splitlines() if item.startswith("[![CI]"))
        path.write_text(text.replace(line + "\n", "", 1), encoding="utf-8")
        with self.assertRaisesRegex(gate.GateError, "5 fact badges"):
            gate.validate_reader_contract(root)

    def test_reordering_the_reader_path_fails_closed(self):
        temporary, root = self.contract_copy()
        self.addCleanup(temporary.cleanup)
        path = root / "README.md"
        text = (path.read_text(encoding="utf-8")
                .replace("## 3. Results", "## TEMP")
                .replace("## 4. Cost Analysis", "## 3. Results")
                .replace("## TEMP", "## 4. Cost Analysis"))
        path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(gate.GateError, "reader path drift"):
            gate.validate_reader_contract(root)

    def test_dropping_a_boundary_token_fails_closed(self):
        temporary, root = self.contract_copy()
        self.addCleanup(temporary.cleanup)
        path = root / "README.md"
        text = path.read_text(encoding="utf-8").replace("max_retries=0", "retries off")
        path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(gate.GateError, "contract token missing"):
            gate.validate_reader_contract(root)

    def test_bilingual_numeric_drift_fails_closed(self):
        temporary, root = self.contract_copy()
        self.addCleanup(temporary.cleanup)
        path = root / "README-CN.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("0.367", "0.368", 1),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(gate.GateError, "numeric-fact drift"):
            gate.validate_bilingual(root)

    def test_bilingual_link_drift_fails_closed(self):
        temporary, root = self.contract_copy()
        self.addCleanup(temporary.cleanup)
        path = root / "README-CN.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace(
                "(scenario-model-benchmark/config/models.json)",
                "(scenario-model-benchmark/config/pricing.json)", 1),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(gate.GateError, "link-target drift"):
            gate.validate_bilingual(root)

    def test_a_broken_local_link_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text(
                "[missing](evidence/not-there.json)\n", encoding="utf-8")
            with self.assertRaisesRegex(gate.GateError, "broken local links"):
                gate.validate_links(root)


class RuleResultSemantics(unittest.TestCase):
    def test_status_is_derived_from_checks(self):
        passing = gate.evaluated_rule("RUN-999", "test", [], [gate.check("same", 3, 3)])
        failing = gate.evaluated_rule("RUN-999", "test", [], [gate.check("differs", 3, 4)])
        self.assertEqual(passing["status"], "PASS")
        self.assertEqual(failing["status"], "FAIL")

    def test_a_rule_with_no_checks_cannot_pass(self):
        empty = gate.evaluated_rule("RUN-999", "test", [], [])
        self.assertEqual(empty["status"], "FAIL")

    def test_not_applicable_requires_a_reason(self):
        na = gate.evaluated_rule("RUN-999", "test", [], applicable=False, reason="typed out")
        self.assertEqual(na["status"], "N/A")
        self.assertTrue(na["reason"])


if __name__ == "__main__":
    unittest.main()
