"""Offline plumbing test for loadtest.py: fake transport, real scheduling and aggregation."""

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harness
import loadtest


def fake_run_one(client, deployment, item, cap, effort, api="responses"):
    time.sleep(0.05)
    fail = item["id"] == "NM02"  # one prompt per cycle is rate-limited
    return {
        "ttft_ms": 100.0, "e2e_ms": 150.0, "decode_ms": 50.0, "tpot_ms": 5.0, "decode_tps": 200.0,
        "prompt_tokens": 0 if fail else 100, "cached_tokens": 0, "completion_tokens": 0 if fail else 30,
        "reasoning_tokens": 0 if fail else 10, "status": None if fail else "completed", "truncated": False,
        "incomplete_reason": None, "finish_reason": None if fail else "stop",
        "model_actually_served": "gpt-5.6-luna-2026-07-09" if deployment.startswith("router") else deployment,
        "routing": {"mode": "balanced", "router_latency_ms": 20} if deployment.startswith("router") else None,
        "response_text": "" if fail else f"answer to {item['id']}",
        "error": "RateLimitError: Error code: 429" if fail else None,
    }


class LoadTestTests(unittest.TestCase):
    def test_parse_arm(self):
        self.assertEqual(loadtest.parse_arm("gpt-5-mini@minimal")["effort"], "minimal")
        self.assertEqual(loadtest.parse_arm("gpt-4o-mini-bench")["effort"], None)
        self.assertEqual(loadtest.parse_arm("router-sol-luna-balanced")["api"], "chat")
        self.assertEqual(loadtest.parse_arm("gpt-5.6-luna@-#chat"), {"deployment": "gpt-5.6-luna", "effort": None, "api": "chat", "arm_id": "gpt-5.6-luna"})
        self.assertEqual(loadtest.classify_error("RateLimitError: Error code: 429"), "429")
        self.assertIsNone(loadtest.classify_error(None))

    def test_levels_run_with_requested_concurrency_and_summaries_close(self):
        in_flight, peak, gate = [0], [0], threading.Lock()

        def tracking(*a, **k):
            with gate:
                in_flight[0] += 1
                peak[0] = max(peak[0], in_flight[0])
            try:
                return fake_run_one(*a, **k)
            finally:
                with gate:
                    in_flight[0] -= 1

        with tempfile.TemporaryDirectory() as tmp:
            argv = ["loadtest.py", "--dataset", str(Path(__file__).resolve().parents[1] / "datasets" / "qira_scenarios.jsonl"),
                    "--arms", "gpt-5.6-luna@none,router-sol-luna-balanced", "--levels", "1,4", "--requests-per-level", "8",
                    "--region", "swedencentral", "--settle-seconds", "0"]
            with patch.object(loadtest, "OUTPUT_DIR", Path(tmp)), patch.object(sys, "argv", argv), \
                 patch.object(harness, "build_client", return_value=(object(), "https://x.example")), \
                 patch.object(harness, "measure_rtt", return_value=1.0), patch.object(harness, "run_one", tracking):
                self.assertEqual(loadtest.main(), 0)
            records = [json.loads(l) for l in next(Path(tmp).glob("loadtest_*.jsonl")).read_text(encoding="utf-8").splitlines()]
            summary = json.loads(next(Path(tmp).glob("loadtest_*.summary.json")).read_text(encoding="utf-8"))
        self.assertEqual(peak[0], 4)
        self.assertEqual(len(records), 2 * (1 + 8 + 8))  # warmup + two levels per arm
        self.assertEqual(sum(r["warmup"] for r in records), 2)
        self.assertEqual(len(summary["summaries"]), 4)
        for s in summary["summaries"]:
            self.assertEqual(s["requests"], 8)
            self.assertEqual(s["ok"] + s["errors"], 8)
            self.assertEqual(s["errors_429"], s["errors"])
            self.assertGreater(s["errors"], 0)  # NM02 is the 2nd prompt, so it appears in every 8-request cycle
            self.assertAlmostEqual(s["aggregate_output_tok_per_s"] * s["wall_s"], 30 * s["ok"], delta=30 * s["ok"] * 0.05)
        router = [s for s in summary["summaries"] if s["arm"] == "router-sol-luna-balanced"]
        self.assertEqual(router[0]["served_families"], ["gpt-5.6-luna"])
        self.assertTrue(all(r["tools_enabled"] is False for r in records))
        self.assertTrue(all(r["error_class"] == "429" for r in records if r["error"]))


if __name__ == "__main__":
    unittest.main()
