"""Fail-closed tests for the public scenario evidence and build inputs."""

import copy
import importlib.util
import json
import lzma
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "scenario_report", ROOT / "scripts" / "build_results_report.py")
REPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(REPORT)


class ScenarioIntegrityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.archive = ROOT / "outputs" / f"evidence_{REPORT.RUN}.json.xz"
        cls.evidence = json.loads(lzma.decompress(cls.archive.read_bytes()))

    def test_executed_and_public_sources_match_pinned_hashes(self):
        REPORT.validate_sources(self.evidence["provenance"])

    def test_changed_archive_source_hash_is_rejected(self):
        changed = copy.deepcopy(self.evidence["provenance"])
        changed["source_sha256"]["harness.py"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "Archive source hashes"):
            REPORT.validate_sources(changed)

    def test_changed_public_build_input_is_rejected(self):
        changed = dict(REPORT.PUBLIC_BUILD_INPUT_SHA256)
        changed["config/pricing.json"] = "0" * 64
        with patch.object(REPORT, "PUBLIC_BUILD_INPUT_SHA256", changed), \
                self.assertRaisesRegex(ValueError, "config/pricing.json"):
            REPORT.validate_sources(self.evidence["provenance"])

    def test_archive_digest_is_the_single_public_digest(self):
        import hashlib
        digest = hashlib.sha256(self.archive.read_bytes()).hexdigest()
        self.assertEqual(set(REPORT.ARCHIVE_SHA256), {digest})

    def test_modified_archive_is_not_a_known_public_variant(self):
        payload = bytearray(self.archive.read_bytes())
        payload[-1] ^= 1
        with tempfile.TemporaryDirectory() as tmp:
            changed = Path(tmp) / self.archive.name
            changed.write_bytes(payload)
            import hashlib
            self.assertNotIn(
                hashlib.sha256(changed.read_bytes()).hexdigest(),
                REPORT.ARCHIVE_SHA256,
            )


if __name__ == "__main__":
    unittest.main()
