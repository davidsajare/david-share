"""Negative and mutation tests for the top-level repository gate."""

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


class RepositoryGateMutations(unittest.TestCase):
    def contract_copy(self) -> tuple[tempfile.TemporaryDirectory, Path]:
        temporary = tempfile.TemporaryDirectory()
        root = Path(temporary.name)
        (root / "evidence").mkdir()
        for relative in (
            "README.md",
            "README-CN.md",
            "evidence/exemplar-alignment.md",
        ):
            source = ROOT / relative
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        return temporary, root

    def test_current_reader_contract_passes(self):
        gate.validate_exemplar_contract(ROOT)
        gate.validate_bilingual(ROOT)

    def test_removing_a_fact_badge_fails_closed(self):
        temporary, root = self.contract_copy()
        self.addCleanup(temporary.cleanup)
        path = root / "README.md"
        text = path.read_text(encoding="utf-8")
        line = next(line for line in text.splitlines() if line.startswith("[![CI]"))
        path.write_text(text.replace(line + "\n", "", 1), encoding="utf-8")
        with self.assertRaisesRegex(gate.GateError, "exactly 5 fact badges"):
            gate.validate_exemplar_contract(root)

    def test_reordering_the_reader_path_fails_closed(self):
        temporary, root = self.contract_copy()
        self.addCleanup(temporary.cleanup)
        path = root / "README.md"
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            "## Live console walkthrough",
            "## TEMP",
        ).replace(
            "## Evidence and executable assets",
            "## Live console walkthrough",
        ).replace(
            "## TEMP",
            "## Evidence and executable assets",
        )
        path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(gate.GateError, "reader path drift"):
            gate.validate_exemplar_contract(root)

    def test_bilingual_numeric_drift_fails_closed(self):
        temporary, root = self.contract_copy()
        self.addCleanup(temporary.cleanup)
        path = root / "README-CN.md"
        text = path.read_text(encoding="utf-8").replace("0.367 s", "0.368 s", 1)
        path.write_text(text, encoding="utf-8")
        with self.assertRaisesRegex(gate.GateError, "numeric-fact drift"):
            gate.validate_bilingual(root)

    def test_a_broken_local_link_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text(
                "[missing](evidence/not-there.json)\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(gate.GateError, "broken local links"):
                gate.validate_links(root)

    def test_rule_status_is_derived_from_checks(self):
        passing = gate.evaluated_rule(
            "RUN-999",
            "test",
            [],
            [gate.check("same", 3, 3)],
        )
        failing = gate.evaluated_rule(
            "RUN-999",
            "test",
            [],
            [gate.check("different", 3, 4)],
        )
        self.assertEqual(passing["status"], "PASS")
        self.assertEqual(failing["status"], "FAIL")
        self.assertFalse(failing["checks"][0]["passed"])

    def test_secret_patterns_ignore_placeholders_but_reject_values(self):
        placeholder = "AZURE_OPENAI_API_KEY=<api-key>"
        realish = "sk-" + "A" * 30
        self.assertFalse(any(pattern.search(placeholder) for pattern in gate.SECRET_PATTERNS.values()))
        self.assertTrue(gate.SECRET_PATTERNS["OpenAI-style key"].search(realish))


if __name__ == "__main__":
    unittest.main()
