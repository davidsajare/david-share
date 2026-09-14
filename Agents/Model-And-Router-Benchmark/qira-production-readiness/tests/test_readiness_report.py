"""Builder tests: full run on the retained evidence plus fail-closed mutations."""

import copy
import importlib.util
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("readiness", ROOT / "scripts" / "build_readiness_report.py")
RB = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RB)


@unittest.skipUnless(RB.RAW.exists() and any(RB.RAW.glob("sessions_*.jsonl")), "production-readiness evidence not retrieved yet")
class ReadinessBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sessions = RB.load_jsonl(RB.RAW / f"sessions_{RB.find_run('sessions')}.jsonl")
        cls.sustained = RB.load_jsonl(RB.raw_path("sustained", RB.find_run("sustained")))
        cls.resilience = RB.load_jsonl(RB.RAW / f"resilience_{RB.find_run('resilience')}.jsonl")

    def with_records(self, prefix, rows, fn):
        real = RB.load_jsonl

        def fake(path):
            return copy.deepcopy(rows) if path.name.startswith(prefix) else real(path)
        with patch.object(RB, "load_jsonl", fake):
            return fn()

    def test_sessions_matrix_and_history(self):
        run, rows, by_turn, by_arm, sequences = RB.sessions_report()
        self.assertEqual(len(rows), len(RB.SESSION_ARMS) * RB.SESSIONS * RB.ITERATIONS * RB.TURNS)
        self.assertEqual(len(by_turn), len(RB.SESSION_ARMS) * RB.TURNS)
        self.assertEqual(len(by_arm), len(RB.SESSION_ARMS))
        self.assertEqual(len(sequences), 2 * RB.SESSIONS * RB.ITERATIONS)
        for a in by_arm:
            self.assertGreater(a["session_cost_vs_4x_turn1"], 0.5)  # sanity: ratio is a meaningful positive number
            self.assertEqual(a["turns_with_cache_hit"] == 0, a["max_reusable_prefix_tokens"] < 1024 or a["turns_with_cache_hit"] == 0)
        for arm in RB.SESSION_ARMS:  # prompt tokens must grow monotonically with turn
            means = [next(r for r in by_turn if r["arm"] == arm and r["turn"] == k)["prompt_tokens_mean"] for k in range(1, 5)]
            self.assertEqual(means, sorted(means))
        # model-change counts must equal what the per-session sequences say
        for arm in ("router-sol-luna-balanced", "router-sol-luna-quality"):
            changed = sum(1 for q in sequences if q["arm"] == arm and q["model_changed_within_session"])
            self.assertEqual(next(a for a in by_arm if a["arm"] == arm)["sessions_with_model_change"], changed)

    def test_sessions_rejects_missing_or_invalid_turns(self):
        for mutate in (lambda rows: rows[:-1],
                       lambda rows: rows[:-1] + [dict(rows[0])],
                       lambda rows: [{**rows[0], "prompt_tokens": 0}] + rows[1:],
                       lambda rows: [{**rows[0], "history_messages": 5}] + rows[1:],
                       lambda rows: [{**rows[0], "region": "eastus"}] + rows[1:],
                       lambda rows: [{**rows[0], "tools_enabled": True}] + rows[1:]):
            with self.subTest(mutate=mutate), self.assertRaises(ValueError):
                self.with_records("sessions", mutate(list(self.sessions)), RB.sessions_report)

    def test_sustained_recomputation_and_window(self):
        run, rows, summary, out = RB.sustained_report()
        self.assertEqual(len(out), len(RB.LOAD_ARMS) * len(RB.LEVELS))
        for r in out:
            self.assertEqual(r["ok"] + r["errors"], r["counted"])
            self.assertLessEqual(r["peak_in_flight"], r["level"])
            self.assertGreaterEqual(r["share_of_window_at_target"], 0.9)  # continuous refill keeps N in flight
        rt = summary["router_429_trace"]
        self.assertEqual(rt["requests"], sum(r["errors_429"] for r in out if r["arm"].startswith("router-")))
        self.assertEqual(set(rt["attempts_histogram"]), {1})  # router never retried on the other model
        with self.assertRaises(ValueError):
            self.with_records("sustained", list(self.sustained)[:-1], RB.sustained_report)
        flipped = [{**r, "counted": not r["counted"]} if r["arm"] == RB.LOAD_ARMS[0] and r["level"] == 4 else r for r in self.sustained]
        with self.assertRaises(ValueError):  # live summary no longer agrees with the records
            self.with_records("sustained", flipped, RB.sustained_report)

    def test_resilience_policies_and_invariants(self):
        run, rows, summary, out = RB.resilience_report()
        self.assertEqual([r["policy"] for r in out], list(RB.POLICIES))
        none, reactive, proactive = out
        self.assertEqual(none["served_by_fallback"], 0)
        self.assertGreater(reactive["success_rate"], none["success_rate"])
        self.assertGreater(reactive["served_by_fallback"], 0)
        self.assertGreater(proactive["proactive_skips"], 0)
        poisoned = [{**r, "served_by_fallback": True, "served_by_deployment": summary["chain"][1]} if r["policy"] == "none" else r for r in self.resilience]
        with self.assertRaises(ValueError):
            self.with_records("resilience", poisoned, RB.resilience_report)

    def test_readmes_generated_and_stable_without_private_values(self):
        import re
        import subprocess
        result = subprocess.run([sys.executable, str(ROOT / "scripts" / "build_readiness_report.py"), "--check"],
                                capture_output=True, text=True, cwd=ROOT, timeout=300)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VERIFIED:", result.stdout)
        for name in ("README.md", "README-CN.md"):
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertNotIn("cognitiveservices.azure.com", text.replace("YOUR-ENDPOINT.cognitiveservices.azure.com", ""))
            self.assertIn("1,024", text)
            # every relative link inside this folder must resolve (cross-PR ../ links are merge-order dependent)
            for target in re.findall(r"\]\(([^)#]+)\)", text):
                if target.startswith(("http://", "https://", "../")):
                    continue
                self.assertTrue((ROOT / target).exists(), f"{name}: broken link {target}")


class LifecycleTests(unittest.TestCase):
    def test_lifecycle_file_shape(self):
        data = json.loads((ROOT / "outputs" / "model_lifecycle_swedencentral.json").read_text(encoding="utf-8"))
        names = {(m["model"], m["version"]) for m in data["models"]}
        self.assertEqual(names, {("gpt-4o-mini", "2024-07-18"), ("gpt-5-mini", "2025-08-07"), ("gpt-5.6-luna", "2026-07-09"),
                                 ("gpt-5.6-sol", "2026-07-09"), ("gpt-5.6-terra", "2026-07-09"), ("model-router", "2025-11-18")})
        for m in data["models"]:
            self.assertRegex(m["inference_retirement_utc"], r"^\d{4}-\d{2}-\d{2}T")
        router = next(m for m in data["models"] if m["model"] == "model-router")
        self.assertEqual(router["capabilities"].get("chatCompletion"), "true")
        self.assertNotEqual(router["capabilities"].get("responses"), "true")


if __name__ == "__main__":
    unittest.main()
