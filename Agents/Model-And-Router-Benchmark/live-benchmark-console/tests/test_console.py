"""Offline tests for the live console: real logic, fake transport, no network."""

import http.client
import json
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import bench_core  # noqa: E402
import server  # noqa: E402
from bench_core import ConsoleError  # noqa: E402


def record(arm="a", **overrides):
    base = {
        "arm": arm, "deployment": arm, "effort": None, "item_id": "NM01",
        "iteration": 1, "warmup": False, "ttft_ms": 500.0, "e2e_ms": 2000.0,
        "decode_ms": 1500.0, "tpot_ms": 10.0, "decode_tps": 100.0,
        "prompt_tokens": 200, "cached_tokens": 0, "completion_tokens": 150,
        "reasoning_tokens": 50, "cost_usd": 0.001, "status": "completed",
        "truncated": False, "error": None, "model_actually_served": "gpt-test",
        "billing_model": "gpt-test",
    }
    base.update(overrides)
    return base


class Statistics(unittest.TestCase):
    def test_percentile_is_nearest_rank(self):
        values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
        self.assertEqual(bench_core.percentile(values, 50), 5)
        self.assertEqual(bench_core.percentile(values, 90), 9)
        self.assertEqual(bench_core.percentile(values, 100), 10)

    def test_percentile_ignores_missing_values(self):
        self.assertEqual(bench_core.percentile([None, 4, None, 8], 50), 4)

    def test_percentile_of_nothing_is_none(self):
        self.assertIsNone(bench_core.percentile([], 50))
        self.assertIsNone(bench_core.percentile([None], 90))


class ArmOrdering(unittest.TestCase):
    def test_study_models_are_grouped_and_efforts_ascend(self):
        arms = [
            "gpt-5.6-luna@xhigh", "gpt-5-mini@high", "gpt-5.6-luna@none",
            "gpt-4o-mini-bench", "gpt-5-mini@medium", "gpt-5.6-luna@max",
            "gpt-5-mini@minimal", "gpt-5.6-luna@high", "gpt-5-mini@low",
            "gpt-5.6-luna@medium", "gpt-5.6-luna@low",
        ]
        self.assertEqual(sorted(arms, key=bench_core.arm_order_key), [
            "gpt-4o-mini-bench",
            "gpt-5-mini@minimal",
            "gpt-5-mini@low",
            "gpt-5-mini@medium",
            "gpt-5-mini@high",
            "gpt-5.6-luna@none",
            "gpt-5.6-luna@low",
            "gpt-5.6-luna@medium",
            "gpt-5.6-luna@high",
            "gpt-5.6-luna@xhigh",
            "gpt-5.6-luna@max",
        ])

    def test_nonstudy_models_remain_grouped_by_deployment(self):
        arms = ["router@low", "other@high", "router", "other@low"]
        self.assertEqual(sorted(arms, key=bench_core.arm_order_key), [
            "other@low", "other@high", "router", "router@low",
        ])


class Summaries(unittest.TestCase):
    def test_errors_are_counted_but_never_averaged(self):
        rows = [record(), record(), record(error="RateLimitError: 429", ttft_ms=None,
                                         prompt_tokens=0)]
        summary = bench_core.summarize_arm("a", rows)
        self.assertEqual(summary["n"], 3)
        self.assertEqual(summary["ok"], 2)
        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["ttft_p50_ms"], 500.0)

    def test_a_stream_without_usage_is_not_counted_as_ok(self):
        summary = bench_core.summarize_arm("a", [record(prompt_tokens=0)])
        self.assertEqual(summary["ok"], 0)
        self.assertIsNone(summary["ttft_p50_ms"])

    def test_truncation_is_reported_separately(self):
        summary = bench_core.summarize_arm("a", [record(), record(truncated=True)])
        self.assertEqual(summary["ok"], 2)
        self.assertEqual(summary["truncated"], 1)

    def test_a_burst_delivery_has_no_per_token_pace(self):
        # decode_ms below the published 50 ms threshold: first and last text
        # delta arrived together, so 12,000 tok/s is an artefact, not a rate.
        rows = [record(decode_ms=1.1, decode_tps=12527.41, tpot_ms=0.08)]
        summary = bench_core.summarize_arm("a", rows)
        self.assertEqual(summary["ok"], 1)
        self.assertEqual(summary["bursts"], 1)
        self.assertEqual(summary["paced_n"], 0)
        self.assertIsNone(summary["decode_tps_p50"])
        self.assertIsNone(summary["tpot_p50_ms"])
        # Latency and cost are still real and still reported.
        self.assertEqual(summary["ttft_p50_ms"], 500.0)
        self.assertIsNotNone(summary["cost_per_1k_requests"])

    def test_burst_records_do_not_contaminate_a_measurable_rate(self):
        rows = [record(decode_ms=1.0, decode_tps=12000.0, tpot_ms=0.1),
                record(decode_ms=1500.0, decode_tps=100.0, tpot_ms=10.0),
                record(decode_ms=1500.0, decode_tps=120.0, tpot_ms=8.0)]
        summary = bench_core.summarize_arm("a", rows)
        self.assertEqual(summary["bursts"], 1)
        self.assertEqual(summary["paced_n"], 2)
        self.assertEqual(summary["decode_tps_p50"], 100.0)
        self.assertAlmostEqual(summary["burst_rate"], 1 / 3, places=3)

    def test_cost_metrics(self):
        summary = bench_core.summarize_arm("a", [record(cost_usd=0.002), record(cost_usd=0.004)])
        self.assertAlmostEqual(summary["cost_usd_mean"], 0.003)
        self.assertAlmostEqual(summary["cost_per_1k_requests"], 3.0)
        # 100 emitted tokens each (150 completion - 50 reasoning), 0.006 total.
        self.assertAlmostEqual(summary["cost_per_1k_emitted_tokens"], 0.006 / 200 * 1000)

    def test_unpriced_arm_reports_no_cost(self):
        summary = bench_core.summarize_arm("a", [record(cost_usd=None)])
        self.assertFalse(summary["priced"])
        self.assertIsNone(summary["cost_per_1k_requests"])
        self.assertIsNone(summary["cost_per_1k_emitted_tokens"])

    def test_served_split_tracks_which_model_answered(self):
        rows = [record(model_actually_served="sol"), record(model_actually_served="luna"),
                record(model_actually_served="luna")]
        summary = bench_core.summarize_arm("router", rows)
        self.assertEqual(summary["served_split"], {"sol": 1, "luna": 2})

    def test_aggregate_throughput_only_with_a_wall_clock(self):
        rows = [record(completion_tokens=150)] * 4
        self.assertIsNone(bench_core.summarize_arm("a", rows)["run_output_tps"])
        timed = bench_core.summarize_arm("a", rows, wall_clock_s=2.0)
        self.assertAlmostEqual(timed["run_output_tps"], 300.0)
        self.assertAlmostEqual(timed["run_rps"], 2.0)

    def test_value_ratio_is_relative_to_the_dearest_arm(self):
        cheap = bench_core.summarize_arm("cheap", [record(cost_usd=0.001)])
        dear = bench_core.summarize_arm("dear", [record(cost_usd=0.010)])
        baseline = dear["cost_per_1k_requests"]
        self.assertEqual(bench_core.value_score(cheap, baseline), 10.0)
        self.assertEqual(bench_core.value_score(dear, baseline), 1.0)


class Assets(unittest.TestCase):
    def test_an_unfetched_lfs_pointer_is_named_as_such(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            pointer = Path(tmp) / "models.json"
            pointer.write_text(
                "version https://git-lfs.github.com/spec/v1\n"
                "oid sha256:0000\nsize 10\n", encoding="utf-8")
            with self.assertRaisesRegex(ConsoleError, "git lfs pull"):
                bench_core.read_json_asset(pointer)

    def test_a_normal_json_asset_still_loads(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "models.json"
            path.write_text('{"models": {"x": {}}}', encoding="utf-8")
            self.assertEqual(bench_core.read_json_asset(path), {"models": {"x": {}}})

    def test_mobile_layout_drops_desktop_chart_minimums(self):
        css = (ROOT / "static" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 700px)", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr)", css)
        self.assertGreater(
            css.rfind("@media (max-width: 700px)"),
            css.find(".charts {"),
            "the mobile override must follow the desktop chart rule in the cascade",
        )
        self.assertRegex(
            css,
            re.compile(r"\.stack\s*\{[^}]*min-width:\s*0", re.DOTALL),
        )


class Catalog(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = bench_core.catalog()

    def test_withheld_prompts_are_not_offered(self):
        ids = {row["id"] for row in self.catalog["scenarios"]}
        self.assertNotIn("PA01", ids)
        self.assertNotIn("PA03", ids)
        self.assertIn("PA02", ids)
        for row in self.catalog["scenarios"] + self.catalog["router_tiers"]:
            self.assertNotIn("[withheld in the public copy", row["text"])

    def test_every_scenario_is_one_of_the_six_assistant_surfaces(self):
        scenarios = {row["scenario"] for row in self.catalog["scenarios"]}
        self.assertEqual(scenarios, set(bench_core.SCENARIO_ORDER))
        for row in self.catalog["scenarios"]:
            self.assertTrue(row["scenario_label"])
            self.assertIn(row["scope"], ("full", "partial"))
            self.assertTrue(row["answer_budget"], row["id"])

    def test_partial_scope_scenarios_explain_what_was_not_covered(self):
        partial = [r for r in self.catalog["scenarios"] if r["scope"] == "partial"]
        self.assertTrue(partial)
        for row in partial:
            self.assertTrue(row["scope_note"], row["id"])

    def test_the_three_study_models_are_flagged(self):
        flagged = {a["deployment"] for a in self.catalog["arms"] if a["is_study_model"]}
        self.assertEqual(flagged, set(bench_core.STUDY_MODELS))

    def test_unverified_registry_only_candidates_are_not_offered(self):
        offered = {a["deployment"] for a in self.catalog["arms"]}
        self.assertTrue(set(bench_core.STUDY_MODELS).issubset(offered))
        self.assertTrue(all(a["verified"] or a["is_study_model"] for a in self.catalog["arms"]))
        self.assertTrue({
            "gpt-5-nano", "gpt-5.4-mini", "gpt-5.4-nano",
        }.isdisjoint(offered))

    def test_deployments_carry_their_verified_region(self):
        self.assertTrue(self.catalog["regions"])
        routers = [a for a in self.catalog["arms"] if a["is_router"]]
        self.assertTrue(all(a["region"] for a in routers))

    def test_router_arms_are_marked_and_use_chat(self):
        routers = [a for a in self.catalog["arms"] if a["is_router"]]
        self.assertTrue(routers, "the registry should contain router deployments")
        for arm in routers:
            self.assertEqual(arm["api"], "chat")
            self.assertIsNone(arm["model_name"])

    def test_direct_arms_resolve_their_billing_price(self):
        arm = next(a for a in self.catalog["arms"] if a["deployment"] == "gpt-5.6-sol-dz")
        self.assertTrue(arm["priced"])
        self.assertEqual(arm["api"], "responses")


class Plans(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = bench_core.catalog()

    def plan(self, **overrides):
        request = {
            "dataset": "scenarios",
            "arms": [{"deployment": "gpt-5.6-luna-dz", "effort": ""}],
            "items": ["NM01"],
            "iterations": 1,
            "concurrency": 1,
            "warmup": False,
        }
        request.update(overrides)
        return bench_core.build_plan(request, self.catalog)

    def test_defaults_come_from_the_registry(self):
        plan = self.plan()
        self.assertEqual(plan.arms[0].effort, "none")
        self.assertEqual(plan.arms[0].api, "responses")

    def test_unknown_deployment_is_rejected(self):
        with self.assertRaises(ConsoleError):
            self.plan(arms=[{"deployment": "gpt-does-not-exist"}])

    def test_unsupported_effort_is_rejected(self):
        with self.assertRaisesRegex(ConsoleError, "does not support reasoning effort"):
            self.plan(arms=[{"deployment": "gpt-5.6-sol-dz", "effort": "max"}])

    def test_no_prompt_selected_is_rejected(self):
        with self.assertRaises(ConsoleError):
            self.plan(items=[])

    def test_no_arm_selected_is_rejected(self):
        with self.assertRaises(ConsoleError):
            self.plan(arms=[])

    def test_oversized_run_is_rejected(self):
        with self.assertRaisesRegex(ConsoleError, "caps a single run"):
            self.plan(arms=[{"deployment": "gpt-5.6-luna-dz"}, {"deployment": "gpt-5.6-sol-dz"}],
                      items=[r["id"] for r in self.catalog["scenarios"]], iterations=20)

    def test_a_router_puts_every_arm_on_chat_completions(self):
        plan = self.plan(arms=[{"deployment": "router-sol-luna-cost"},
                               {"deployment": "gpt-5.6-sol-dz"}])
        self.assertEqual({a.api for a in plan.arms}, {"chat"})

    def test_effort_is_never_forwarded_to_a_router(self):
        plan = self.plan(arms=[{"deployment": "router-sol-luna-cost", "effort": "high"}])
        self.assertIsNone(plan.arms[0].effort)

    def test_warmup_adds_one_pass_per_prompt(self):
        plan = self.plan(items=["NM01", "NM02"], iterations=3, warmup=True)
        self.assertEqual(plan.measured_calls, 6)
        self.assertEqual(plan.total_calls, 8)

    def test_each_prompt_is_capped_at_its_own_budget_plus_headroom(self):
        plan = self.plan(items=["NM01", "NM03"], headroom=8192)
        by_id = {i["id"]: i for i in plan.items}
        # NM01 budget 400, NM03 budget 80 in the study dataset.
        self.assertEqual(plan.cap_for(by_id["NM01"]), 400 + 8192)
        self.assertEqual(plan.cap_for(by_id["NM03"]), 80 + 8192)

    def test_without_headroom_a_flat_cap_is_used(self):
        plan = self.plan(items=["NM01"], max_output_tokens=512)
        self.assertIsNone(plan.headroom)
        self.assertEqual(plan.cap_for(plan.items[0]), 512)

    def test_headroom_is_clamped(self):
        self.assertEqual(self.plan(headroom=999999).headroom, 32768)

    def test_zero_headroom_keeps_the_prompt_answer_budget(self):
        plan = self.plan(items=["NM01"], headroom=0)
        self.assertEqual(plan.headroom, 0)
        self.assertEqual(plan.cap_for(plan.items[0]), 400)


class FakeHarness:
    """Stands in for harness.py: same call signature, deterministic output."""

    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def run_one(self, client, deployment, item, max_output_tokens, effort, api="responses"):
        self.calls.append((deployment, item["id"], effort, api))
        if item["id"] in self.fail_on:
            return {"ttft_ms": None, "e2e_ms": 1.0, "prompt_tokens": 0, "cached_tokens": 0,
                    "completion_tokens": 0, "reasoning_tokens": 0, "status": None,
                    "truncated": False, "incomplete_reason": None, "decode_ms": None,
                    "tpot_ms": None, "decode_tps": None, "model_actually_served": None,
                    "routing": None, "response_text": "", "error": "APIError: boom"}
        return {"ttft_ms": 400.0, "e2e_ms": 1200.0, "decode_ms": 800.0, "tpot_ms": 8.0,
                "decode_tps": 125.0, "prompt_tokens": 300, "cached_tokens": 0,
                "completion_tokens": 120, "reasoning_tokens": 20, "status": "completed",
                "truncated": False, "incomplete_reason": None,
                "model_actually_served": "gpt-5.6-luna", "routing": None,
                "response_text": "hello", "error": None}

    @staticmethod
    def billing_model_name(served):
        return served

    @staticmethod
    def compute_cost(pricing, model, prompt, cached, completion, registry=None):
        return round(prompt * 1e-6 + completion * 5e-6, 8)


class Execution(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = bench_core.catalog()

    def run_plan(self, fake, **overrides):
        request = {
            "dataset": "scenarios",
            "arms": [{"deployment": "gpt-5.6-luna-dz"}],
            "items": ["NM01", "NM02"],
            "iterations": 2,
            "concurrency": 1,
            "warmup": True,
        }
        request.update(overrides)
        plan = bench_core.build_plan(request, self.catalog)
        events = []
        cancel = threading.Event()
        records = bench_core.execute_plan(
            plan, client=None, pricing={}, registry=bench_core.load_registry(),
            harness=fake, emit=lambda kind, payload: events.append((kind, payload)),
            cancel=cancel)
        return plan, records, events

    def test_warmup_results_are_measured_but_not_scored(self):
        fake = FakeHarness()
        plan, records, events = self.run_plan(fake)
        self.assertEqual(len(fake.calls), plan.total_calls)      # 2 prompts x 3 passes
        self.assertEqual(len(records), plan.measured_calls)      # warm-up excluded
        emitted = [p for kind, p in events if kind == "record"]
        self.assertEqual(len(emitted), plan.total_calls)
        self.assertEqual(sum(1 for p in emitted if p["warmup"]), 2)

    def test_every_record_carries_its_arm_and_cost(self):
        _, records, _ = self.run_plan(FakeHarness())
        for row in records:
            self.assertEqual(row["arm"], "gpt-5.6-luna-dz@none")
            self.assertEqual(row["billing_model"], "gpt-5.6-luna")
            self.assertAlmostEqual(row["cost_usd"], 300 * 1e-6 + 120 * 5e-6)
            self.assertNotIn("response_text", row)
            self.assertEqual(row["preview"], "hello")

    def test_an_arm_summary_is_emitted_per_arm(self):
        _, _, events = self.run_plan(
            FakeHarness(), arms=[{"deployment": "gpt-5.6-luna-dz"},
                                 {"deployment": "gpt-5.6-sol-dz"}])
        summaries = [p for kind, p in events if kind == "arm_summary"]
        self.assertEqual(len(summaries), 2)
        self.assertEqual({s["arm"] for s in summaries},
                         {"gpt-5.6-luna-dz@none", "gpt-5.6-sol-dz@none"})

    def test_a_failing_call_lands_in_the_summary_as_an_error(self):
        _, _, events = self.run_plan(FakeHarness(fail_on={"NM02"}))
        summary = next(p for kind, p in events if kind == "arm_summary")
        self.assertEqual(summary["errors"], 2)
        self.assertEqual(summary["ok"], 2)
        self.assertTrue(summary["error_samples"])

    def test_concurrency_is_passed_through_without_changing_the_record_count(self):
        fake = FakeHarness()
        plan, records, _ = self.run_plan(fake, concurrency=4)
        self.assertEqual(plan.concurrency, 4)
        self.assertEqual(len(records), plan.measured_calls)

    def test_warmup_duration_is_excluded_from_measured_throughput(self):
        fake = FakeHarness()
        # warm-up starts at 0; measured pass starts at 100 and ends at 102.
        with patch.object(bench_core.time, "perf_counter", side_effect=[0.0, 100.0, 102.0]):
            _, _, events = self.run_plan(
                fake, items=["NM01"], iterations=1, concurrency=2, warmup=True)
        summary = next(payload for kind, payload in events if kind == "arm_summary")
        self.assertEqual(summary["run_output_tps"], 60.0)  # 120 measured tokens / 2 seconds
        self.assertEqual(summary["run_rps"], 0.5)


class Totals(unittest.TestCase):
    def test_arm_totals_are_the_measured_sums(self):
        rows = [record(prompt_tokens=200, completion_tokens=150, reasoning_tokens=50,
                       cost_usd=0.002),
                record(prompt_tokens=300, completion_tokens=100, reasoning_tokens=20,
                       cost_usd=0.003)]
        summary = bench_core.summarize_arm("a", rows)
        self.assertEqual(summary["prompt_tokens_total"], 500)
        self.assertEqual(summary["output_tokens_total"], 250)
        self.assertEqual(summary["reasoning_tokens_total"], 70)
        self.assertEqual(summary["total_tokens"], 750)
        self.assertAlmostEqual(summary["cost_usd_total"], 0.005)

    def test_run_totals_add_up_across_arms(self):
        a = bench_core.summarize_arm("a", [record(cost_usd=0.001)])
        b = bench_core.summarize_arm("b", [record(cost_usd=0.002)])
        totals = server.run_totals([a, b])
        self.assertEqual(totals["prompt_tokens"], 400)
        self.assertEqual(totals["output_tokens"], 300)
        self.assertEqual(totals["total_tokens"], 700)
        self.assertAlmostEqual(totals["cost_usd"], 0.003)
        self.assertTrue(totals["cost_complete"])

    def test_an_unpriced_arm_makes_the_run_cost_partial(self):
        a = bench_core.summarize_arm("a", [record(cost_usd=0.001)])
        b = bench_core.summarize_arm("b", [record(cost_usd=None)])
        totals = server.run_totals([a, b])
        self.assertAlmostEqual(totals["cost_usd"], 0.001)
        self.assertFalse(totals["cost_complete"])

    def test_totals_of_nothing_are_zero_not_a_crash(self):
        totals = server.run_totals([])
        self.assertEqual(totals["total_tokens"], 0)
        self.assertIsNone(totals["cost_usd"])
        self.assertFalse(totals["cost_complete"])


class History(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._original = server.HISTORY
        server.HISTORY = Path(self._tmp.name)

    def tearDown(self):
        server.HISTORY = self._original
        self._tmp.cleanup()

    def make_state(self, run_id="abc123de"):
        catalog_data = bench_core.catalog()
        plan = bench_core.build_plan({
            "dataset": "scenarios",
            "arms": [{"deployment": "gpt-5.6-luna-dz"}],
            "items": ["NM01"], "iterations": 1, "warmup": False,
        }, catalog_data)
        summary = bench_core.summarize_arm("gpt-5.6-luna-dz@none", [record(cost_usd=0.002)])
        return {
            "run_id": run_id, "plan": plan, "cancel": threading.Event(),
            "records": [record()], "summaries": [summary],
            "started_at": time.time() - 5, "finished_at": time.time(),
        }

    def test_a_finished_run_is_saved_and_can_be_reopened(self):
        path = server.save_history(self.make_state())
        self.assertIsNotNone(path)
        self.assertTrue(path.is_file())
        index = server.history_index()
        self.assertEqual(len(index), 1)
        self.assertEqual(index[0]["run_id"], "abc123de")
        self.assertEqual(index[0]["totals"]["prompt_tokens"], 200)
        full = server.history_run("abc123de")
        self.assertEqual(len(full["rows"]), 1)
        self.assertEqual(full["summaries"][0]["arm"], "gpt-5.6-luna-dz@none")

    def test_each_run_is_stored_separately(self):
        server.save_history(self.make_state("aaaaaaaa"))
        server.save_history(self.make_state("bbbbbbbb"))
        self.assertEqual(len({e["run_id"] for e in server.history_index()}), 2)

    def test_a_run_with_no_summaries_is_not_saved(self):
        state = self.make_state()
        state["summaries"] = []
        self.assertIsNone(server.save_history(state))
        self.assertEqual(server.history_index(), [])

    def test_a_saved_run_can_be_deleted(self):
        server.save_history(self.make_state("ccccccc1"))
        self.assertTrue(server.delete_history_run("ccccccc1"))
        self.assertEqual(server.history_index(), [])

    def test_a_traversal_run_id_is_refused(self):
        self.assertIsNone(server.history_run("../../etc/passwd"))
        self.assertFalse(server.delete_history_run("../../etc/passwd"))

    def test_history_survives_a_corrupt_file(self):
        server.save_history(self.make_state("dddddddd"))
        (server.HISTORY / "run_20260101_000000_eeeeeeee.json").write_text("{not json",
                                                                         encoding="utf-8")
        self.assertEqual(len(server.history_index()), 1)


class FakeRunnerHandler(BaseHTTPRequestHandler):
    record = {
        "run_id": "feedface",
        "started_at": "2026-09-11T12:00:00+00:00",
        "finished_at": "2026-09-11T12:00:01+00:00",
        "duration_s": 1.0,
        "mode": "live",
        "dataset": "scenarios",
        "arms": ["gpt-5.6-luna@none"],
        "items": ["NM01"],
        "iterations": 1,
        "concurrency": 1,
        "records": 1,
        "totals": {"total_tokens": 10, "cost_usd": 0.001},
        "summaries": [],
        "rows": [{"item_id": "NM01"}],
    }
    # A second run that is still executing until a test marks it finished.
    late_record = dict(record, run_id="cafebabe",
                       totals={"total_tokens": 20, "cost_usd": 0.002})
    late_ready = False
    slow_detail = 0.0
    posts: list[tuple[str, bytes]] = []

    def log_message(self, *_):
        pass

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        for _ in range(40):
            try:
                self.wfile.write(b'data: {"type": "record"}\n\n')
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                return
            time.sleep(0.05)

    def do_GET(self):  # noqa: N802
        if self.path.startswith("/api/events"):
            self._stream()
        elif self.path == "/api/truncated-error":
            # Error headers, then a body that stops short of Content-Length.
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", "100")
            self.end_headers()
            self.wfile.write(b"{}")
            self.close_connection = True
        elif self.path == "/api/history":
            runs = [{"run_id": "feedface"}]
            if type(self).late_ready:
                runs.append({"run_id": "cafebabe"})
            self._json(200, {"runs": runs})
        elif self.path == "/api/history/feedface":
            if type(self).slow_detail:
                time.sleep(type(self).slow_detail)
            self._json(200, self.record)
        elif self.path == "/api/history/cafebabe" and type(self).late_ready:
            self._json(200, self.late_record)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        type(self).posts.append((self.path, body))
        self._json(200, {"run_id": "feedface"})


class RemoteRunner(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeRunnerHandler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        self.original_url = server.RUNNER_URL
        self.original_history = server.HISTORY
        self.tmp = tempfile.TemporaryDirectory()
        server.RUNNER_URL = self.url
        server.HISTORY = Path(self.tmp.name)
        server._reconciled_at = 0.0
        server._migrated = False
        server._mirroring.clear()
        FakeRunnerHandler.late_ready = False
        FakeRunnerHandler.posts.clear()

    def tearDown(self):
        self._drain_mirrors()
        server.RUNNER_URL = self.original_url
        server.HISTORY = self.original_history
        server._mirroring.clear()
        FakeRunnerHandler.late_ready = False
        self.tmp.cleanup()

    def _wait_for(self, run_id, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if server.mirrored_run_path(run_id) is not None:
                return True
            time.sleep(0.02)
        return False

    def _drain_mirrors(self, timeout=5.0):
        deadline = time.monotonic() + timeout
        while server._mirroring and time.monotonic() < deadline:
            time.sleep(0.02)

    def test_remote_runner_makes_the_portal_live_without_a_local_endpoint(self):
        with patch.dict("os.environ", {"AZURE_OPENAI_ENDPOINT": ""}):
            self.assertTrue(server.runner_configured())
            self.assertEqual(server.server_mode(), "live")

    def test_runner_json_forwards_a_run_request(self):
        status, payload = server.runner_json("POST", "/api/run", {"items": ["NM01"]})
        self.assertEqual(status, 200)
        self.assertEqual(payload["run_id"], "feedface")

    def test_completed_runner_history_is_mirrored_locally(self):
        path = server.import_runner_history("feedface")
        self.assertTrue(path.is_file())
        self.assertEqual(server.history_run("feedface")["totals"]["total_tokens"], 10)

    def test_invalid_remote_history_is_not_imported(self):
        self.assertIsNone(server.import_runner_history("deadbeef"))
        self.assertEqual(server.history_index(), [])

    def test_importing_the_same_run_twice_keeps_one_row(self):
        self.assertIsNotNone(server.import_runner_history("feedface"))
        self.assertIsNone(server.import_runner_history("feedface"))
        self.assertEqual([r["run_id"] for r in server.history_index()], ["feedface"])

    def test_a_finished_run_is_mirrored_with_no_browser_attached(self):
        # The whole point: nothing here opens /api/events.
        with patch.multiple(server, MIRROR_POLL_SECONDS=0.02,
                            MIRROR_POLL_MAX_SECONDS=0.05,
                            MIRROR_DEADLINE_SECONDS=5):
            self.assertTrue(server.mirror_runner_run("cafebabe"))
            self.assertFalse(self._wait_for("cafebabe", timeout=0.3))
            FakeRunnerHandler.late_ready = True
            self.assertTrue(self._wait_for("cafebabe"))
            self._drain_mirrors()
        self.assertEqual(server.history_run("cafebabe")["totals"]["total_tokens"], 20)

    def test_mirroring_is_started_once_per_run(self):
        with patch.object(server.threading, "Thread") as thread:
            self.assertTrue(server.mirror_runner_run("cafebabe"))
            self.assertFalse(server.mirror_runner_run("cafebabe"))
        self.assertEqual(thread.call_count, 1)
        server._mirroring.clear()

    def test_mirroring_reports_failure_if_no_worker_could_start(self):
        with patch.object(server.threading, "Thread", side_effect=RuntimeError):
            self.assertFalse(server.mirror_runner_run("cafebabe"))
        # The run id must not stay wedged in the in-flight set.
        self.assertNotIn("cafebabe", server._mirroring)

    def test_mirroring_rejects_a_run_id_the_runner_did_not_produce(self):
        self.assertFalse(server.mirror_runner_run(""))
        self.assertFalse(server.mirror_runner_run("../../etc/passwd"))
        self.assertFalse(server.mirror_runner_run("feedface\n"))
        self.assertIsNone(server.mirrored_run_path("feedface\n"))

    def test_reconcile_backfills_runs_the_portal_never_saw(self):
        FakeRunnerHandler.late_ready = True
        self.assertEqual(server.reconcile_runner_history(force=True), 2)
        self.assertEqual({r["run_id"] for r in server.history_index()},
                         {"feedface", "cafebabe"})
        # Nothing left to pull, and no duplicate rows.
        self.assertEqual(server.reconcile_runner_history(force=True), 0)
        self.assertEqual(len(server.history_index()), 2)

    def test_reconcile_is_throttled_between_history_reads(self):
        self.assertEqual(server.reconcile_runner_history(force=True), 1)
        FakeRunnerHandler.late_ready = True
        self.assertEqual(server.reconcile_runner_history(), 0)
        self.assertEqual(server.reconcile_runner_history(force=True), 1)

    def test_reconcile_survives_an_offline_runner(self):
        with patch.object(server, "RUNNER_URL", "http://127.0.0.1:1"):
            self.assertEqual(server.reconcile_runner_history(force=True), 0)

    def _legacy_file(self, run_id, suffix, **extra):
        # How mirrors were named before the run id went into the file name.
        path = server.HISTORY / f"run_20260911_120000_{suffix}.json"
        path.write_text(json.dumps(dict(FakeRunnerHandler.record,
                                        run_id=run_id, **extra)),
                        encoding="utf-8")
        return path

    def test_a_legacy_mirror_is_renamed_instead_of_imported_twice(self):
        legacy = self._legacy_file("feedface", "0a1206eee27a8bc5")
        self.assertEqual(server.reconcile_runner_history(force=True), 0)
        self.assertFalse(legacy.exists())
        self.assertEqual([r["run_id"] for r in server.history_index()], ["feedface"])
        self.assertEqual(server.history_run("feedface")["totals"]["total_tokens"], 10)

    def test_migration_drops_a_legacy_duplicate_of_a_stored_run(self):
        server.import_runner_history("feedface")
        self._legacy_file("feedface", "0a1206eee27a8bc5", totals={"total_tokens": 99})
        self.assertEqual(len(server.history_index()), 2)
        self.assertEqual(server.migrate_history_filenames(), 1)
        self.assertEqual([r["run_id"] for r in server.history_index()], ["feedface"])
        # The correctly named record is the one that survives.
        self.assertEqual(server.history_run("feedface")["totals"]["total_tokens"], 10)

    def test_migration_is_idempotent_and_leaves_good_names_alone(self):
        server.import_runner_history("feedface")
        before = sorted(p.name for p in server.HISTORY.glob("run_*.json"))
        self.assertEqual(server.migrate_history_filenames(), 0)
        self.assertEqual(server.migrate_history_filenames(), 0)
        self.assertEqual(sorted(p.name for p in server.HISTORY.glob("run_*.json")),
                         before)

    def test_migration_ignores_files_it_cannot_parse(self):
        broken = server.HISTORY / "run_20260101_000000_eeeeeeee.json"
        broken.write_text("{not json", encoding="utf-8")
        self.assertEqual(server.migrate_history_filenames(), 0)
        self.assertTrue(broken.exists())

    def test_a_run_older_than_retention_is_left_on_the_runner(self):
        # Writing it would trip oldest-first pruning, and the next reconcile
        # would fetch and drop it again once a minute, for ever.
        for i in range(3):
            (server.HISTORY / f"run_20270101_00000{i}_{'b' * 15}{i}.json").write_text(
                json.dumps(dict(FakeRunnerHandler.record, run_id=f"{'b' * 15}{i}")),
                encoding="utf-8")
        with patch.object(server, "HISTORY_RETENTION", 3):
            self.assertIsNone(server.import_runner_history("feedface"))
            self.assertEqual(server.reconcile_runner_history(force=True), 0)
        self.assertEqual(len(server.history_index()), 3)
        self.assertIsNone(server.mirrored_run_path("feedface"))

    def test_a_recent_run_is_still_imported_at_retention(self):
        for i in range(3):
            (server.HISTORY / f"run_20200101_00000{i}_{'a' * 15}{i}.json").write_text(
                json.dumps(dict(FakeRunnerHandler.record, run_id=f"{'a' * 15}{i}")),
                encoding="utf-8")
        with patch.object(server, "HISTORY_RETENTION", 3):
            self.assertIsNotNone(server.import_runner_history("feedface"))
        self.assertIn("feedface", {r["run_id"] for r in server.history_index()})

    def test_a_deleted_run_is_not_resurrected_by_the_next_sync(self):
        self.assertEqual(server.reconcile_runner_history(force=True), 1)
        self.assertTrue(server.delete_history_run("feedface"))
        self.assertEqual(server.history_index(), [])
        self.assertEqual(server.reconcile_runner_history(force=True), 0)
        self.assertEqual(server.history_index(), [])
        self.assertIsNone(server.import_runner_history("feedface"))

    def test_a_deleted_run_is_not_mirrored_again(self):
        server.import_runner_history("feedface")
        server.delete_history_run("feedface")
        self.assertTrue(server.run_is_deleted("feedface"))
        self.assertFalse(server.mirror_runner_run("feedface"))

    def test_deleting_leaves_no_measurements_behind(self):
        server.import_runner_history("feedface")
        server.delete_history_run("feedface")
        leftovers = list(server.HISTORY.glob("run_*"))
        self.assertEqual([p.suffix for p in leftovers], [".deleted"])
        self.assertEqual(leftovers[0].read_bytes(), b"")

    def test_worker_count_is_capped(self):
        with patch.object(server.threading, "Thread") as thread:
            started = sum(1 for i in range(server.MAX_MIRROR_WORKERS + 5)
                          if server.mirror_runner_run(f"{i:016x}"))
        self.assertEqual(started, server.MAX_MIRROR_WORKERS)
        self.assertEqual(thread.call_count, server.MAX_MIRROR_WORKERS)
        server._mirroring.clear()

    def test_history_is_served_when_the_runner_stalls_mid_response(self):
        # A wedged tunnel accepts the connection and then never answers, so the
        # failure lands in the read as TimeoutError, not URLError.
        server.import_runner_history("feedface")
        stalled = socket.socket()
        stalled.bind(("127.0.0.1", 0))
        stalled.listen(1)
        try:
            with patch.object(server, "RUNNER_URL",
                              f"http://127.0.0.1:{stalled.getsockname()[1]}"), \
                    patch.object(server, "RECONCILE_TIMEOUT_SECONDS", 1):
                self.assertEqual(server.reconcile_runner_history(force=True), 0)
        finally:
            stalled.close()
        # The point of the test: local records are still readable.
        self.assertEqual([r["run_id"] for r in server.history_index()], ["feedface"])

    def test_history_paths_cannot_leave_the_history_folder(self):
        self.assertIsNotNone(server.history_path("run_20260101_000000_feedface.json"))
        for escape in ("../evil.json", "../../evil.json", "sub/evil.json",
                       "..", "", "."):
            self.assertIsNone(server.history_path(escape), escape)

    def test_a_stalled_runner_is_reported_as_a_console_error(self):
        stalled = socket.socket()
        stalled.bind(("127.0.0.1", 0))
        stalled.listen(1)
        try:
            with patch.object(server, "RUNNER_URL",
                              f"http://127.0.0.1:{stalled.getsockname()[1]}"):
                with self.assertRaises(ConsoleError):
                    server.runner_json("GET", "/api/history", timeout=1)
        finally:
            stalled.close()

    def test_closing_the_tab_does_not_cancel_the_run(self):
        # Two real consoles, as deployed: a portal relaying a runner's stream.
        # The fake runner cannot show this - the cancel lived in the runner's
        # own disconnect handler, reached by the relay closing its upstream.
        state = {"cancel": threading.Event(), "events": queue.Queue(),
                 "records": [], "summaries": [], "started_at": time.time(),
                 "finished_at": None, "error": None, "run_id": "feedface"}
        runner = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        runner.daemon_threads = True
        threading.Thread(target=runner.serve_forever, daemon=True).start()
        portal = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        portal.daemon_threads = True
        threading.Thread(target=portal.serve_forever, daemon=True).start()
        feeder = threading.Event()

        def keep_streaming():
            while not feeder.is_set():
                state["events"].put({"type": "record", "ttft_ms": 1.0})
                time.sleep(0.05)

        threading.Thread(target=keep_streaming, daemon=True).start()
        try:
            with patch.dict(server._runs, {"feedface": state}), \
                    patch.object(server, "RUNNER_URL",
                                 f"http://127.0.0.1:{runner.server_address[1]}"):
                client = socket.create_connection(
                    ("127.0.0.1", portal.server_address[1]), timeout=5)
                client.sendall(b"GET /api/events?run_id=feedface HTTP/1.1\r\n"
                               b"Host: 127.0.0.1\r\n\r\n")
                self.assertTrue(client.recv(64))
                client.close()  # the tab goes away mid-stream
                time.sleep(1.5)
        finally:
            feeder.set()
            portal.shutdown(); portal.server_close()
            runner.shutdown(); runner.server_close()
        self.assertFalse(state["cancel"].is_set(),
                         "losing the browser must not cancel the measurement")
        self.assertEqual([p for p, _ in FakeRunnerHandler.posts if "cancel" in p], [])

    def test_a_delete_that_races_an_import_still_wins(self):
        # The import is in flight when the operator deletes the run.
        server.import_runner_history("feedface")
        self.assertTrue(server.delete_history_run("feedface"))
        self.assertIsNone(server.import_runner_history("feedface"))
        self.assertEqual(server.history_index(), [])

    def test_a_record_written_beside_a_marker_does_not_survive(self):
        server.import_runner_history("feedface")
        landed = next(iter(server.HISTORY.glob("run_*_feedface.json")))
        server.delete_history_run("feedface")
        # Simulate the racing writer that slipped in before the lock existed.
        landed.write_text(json.dumps(FakeRunnerHandler.record), encoding="utf-8")
        server._prune_history()
        self.assertEqual(server.history_index(), [])
        self.assertTrue(server.run_is_deleted("feedface"))

    def test_convergence_matches_the_run_not_the_stamp(self):
        # A racing import whose started_at was unparseable files under today's
        # stamp, so the marker written at the record's own stamp has a
        # different stem and matching on the stem alone would miss it.
        server.import_runner_history("feedface")
        server.delete_history_run("feedface")
        marker = next(iter(server.HISTORY.glob("run_*_feedface.deleted")))
        stray = server.HISTORY / "run_20991231_235959_feedface.json"
        stray.write_text(json.dumps(FakeRunnerHandler.record), encoding="utf-8")
        self.assertNotEqual(marker.stem, stray.stem)
        server._prune_history()
        self.assertFalse(stray.exists())
        self.assertEqual(server.history_index(), [])

    def test_reconcile_stops_inside_its_wall_clock_budget(self):
        FakeRunnerHandler.late_ready = True
        with patch.object(server, "RECONCILE_BUDGET_SECONDS", 0), \
                patch.object(server, "RECONCILE_TIMEOUT_SECONDS", 5):
            started = time.monotonic()
            self.assertEqual(server.reconcile_runner_history(force=True), 0)
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(server.history_index(), [])

    def test_a_request_started_near_the_deadline_cannot_overrun_it(self):
        # The budget is only real if each request is clamped to what is left.
        FakeRunnerHandler.slow_detail = 4.0
        try:
            with patch.object(server, "RECONCILE_BUDGET_SECONDS", 0.2), \
                    patch.object(server, "RECONCILE_TIMEOUT_SECONDS", 5):
                started = time.monotonic()
                server.reconcile_runner_history(force=True)
                elapsed = time.monotonic() - started
        finally:
            FakeRunnerHandler.slow_detail = 0.0
        self.assertLess(elapsed, 3.0, f"reconcile overran its budget: {elapsed:.2f}s")

    def test_a_delete_landing_mid_import_still_wins(self):
        # The real race: another importer stores the record and the operator
        # deletes it while this import's fetch is still in flight. Only a
        # re-check under the write lock can keep it from being written back.
        real = server.runner_json

        def race(method, path, payload=None, timeout=30):
            result = real(method, path, payload, timeout)
            if path.startswith("/api/history/"):
                (server.HISTORY / "run_20260911_120000_feedface.json").write_text(
                    json.dumps(FakeRunnerHandler.record), encoding="utf-8")
                server.delete_history_run("feedface")
            return result

        with patch.object(server, "runner_json", race):
            self.assertIsNone(server.import_runner_history("feedface"),
                              "a deleted run must not report itself as stored")
        self.assertEqual(server.history_index(), [])
        self.assertTrue(server.run_is_deleted("feedface"))

    def test_a_delete_that_cannot_be_recorded_is_reported_as_failed(self):
        server.import_runner_history("feedface")
        with patch.object(server.Path, "touch", side_effect=OSError):
            self.assertFalse(server.delete_history_run("feedface"))
        # The measurements are still there rather than silently gone.
        self.assertEqual([r["run_id"] for r in server.history_index()], ["feedface"])

    def test_reconcile_gives_every_request_the_short_timeout(self):
        seen = []
        real = server.runner_json

        def spy(method, path, payload=None, timeout=30):
            seen.append((path, timeout))
            return real(method, path, payload, timeout)

        with patch.object(server, "runner_json", spy):
            server.reconcile_runner_history(force=True)
        self.assertTrue(seen)
        self.assertEqual({t for _, t in seen}, {server.RECONCILE_TIMEOUT_SECONDS})

    def test_reconcile_does_not_refetch_records_retention_would_drop(self):
        FakeRunnerHandler.late_ready = True
        for i in range(3):
            (server.HISTORY / f"run_20270101_00000{i}_{'b' * 15}{i}.json").write_text(
                json.dumps(dict(FakeRunnerHandler.record, run_id=f"{'b' * 15}{i}")),
                encoding="utf-8")
        seen = []
        real = server.runner_json

        def spy(method, path, payload=None, timeout=30):
            seen.append(path)
            return real(method, path, payload, timeout)

        with patch.object(server, "HISTORY_RETENTION", 3), \
                patch.object(server, "runner_json", spy):
            self.assertEqual(server.reconcile_runner_history(force=True), 0)
        self.assertEqual([p for p in seen if p.startswith("/api/history/")], [])

    def test_a_truncated_error_body_does_not_fail_the_caller(self):
        status, payload = server.runner_json("GET", "/api/truncated-error")
        self.assertEqual(status, 502)
        self.assertIn("error", payload)


class Export(unittest.TestCase):
    def test_csv_has_a_stable_header_and_ignores_extra_fields(self):
        csv_text = server.records_to_csv([record(extra="ignored")])
        lines = csv_text.strip().splitlines()
        self.assertEqual(lines[0], ",".join(server.CSV_COLUMNS))
        self.assertEqual(len(lines), 2)
        self.assertNotIn("ignored", csv_text)

    def test_csv_of_nothing_is_still_a_valid_header(self):
        self.assertEqual(server.records_to_csv([]).strip(), ",".join(server.CSV_COLUMNS))


class Replay(unittest.TestCase):
    def test_the_committed_pack_matches_the_recorded_runs(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "build_replay_pack.py"), "--check"],
            capture_output=True, text=True, cwd=ROOT)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("VERIFIED", result.stdout)

    def test_every_replayed_arm_reports_the_headline_metrics(self):
        pack = server.load_replay()
        self.assertTrue(pack["available"])
        self.assertTrue(pack["runs"])
        for run in pack["runs"]:
            self.assertTrue(run["summaries"], run["id"])
            for summary in run["summaries"]:
                for key in ("ttft_p50_ms", "decode_tps_p50", "cost_per_1k_requests",
                            "output_tokens_mean", "ok"):
                    self.assertIn(key, summary)

    def test_scenario_replay_uses_model_then_effort_order(self):
        pack = server.load_replay()
        run = next(r for r in pack["runs"] if r["id"] == "scenario-matrix")
        self.assertEqual([summary["arm"] for summary in run["summaries"]], [
            "gpt-4o-mini-bench",
            "gpt-5-mini@minimal",
            "gpt-5-mini@low",
            "gpt-5-mini@medium",
            "gpt-5-mini@high",
            "gpt-5.6-luna@none",
            "gpt-5.6-luna@low",
            "gpt-5.6-luna@medium",
            "gpt-5.6-luna@high",
            "gpt-5.6-luna@xhigh",
            "gpt-5.6-luna@max",
        ])

    def test_replay_catalog_hides_unverified_registry_only_entries(self):
        offered = {
            arm["deployment"] for arm in server.load_replay()["catalog"]["arms"]
        }
        self.assertTrue({
            "gpt-5-nano", "gpt-5.4-mini", "gpt-5.4-nano",
        }.isdisjoint(offered))

    def test_pack_embeds_catalog_for_a_clone_without_git_lfs(self):
        pack = server.load_replay()
        self.assertTrue(pack["catalog"]["arms"])
        self.assertTrue(pack["catalog"]["scenarios"])
        with patch.object(bench_core, "catalog", side_effect=ConsoleError("LFS pointer")), \
                patch.object(server, "server_mode", return_value="replay"):
            self.assertEqual(server.load_catalog(), pack["catalog"])

    def test_remote_portal_can_use_embedded_catalog_without_sibling_assets(self):
        pack = server.load_replay()
        with patch.object(bench_core, "catalog", side_effect=ConsoleError("missing siblings")), \
                patch.object(server, "RUNNER_URL", "http://127.0.0.1:8514"):
            self.assertEqual(server.load_catalog(), pack["catalog"])


class HttpSurface(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.server.daemon_threads = True
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def get(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        connection.request("GET", path)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        return response.status, body

    def post(self, path, payload, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        body = json.dumps(payload)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        connection.request("POST", path, body=body, headers=request_headers)
        response = connection.getresponse()
        text = response.read().decode("utf-8")
        connection.close()
        return response.status, json.loads(text)

    def test_index_is_served(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("Live model benchmark console", body)

    def test_index_initializes_the_shared_light_theme(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn('get("clawpilotTheme")', body)
        self.assertIn('setAttribute("data-theme", theme)', body)
        status, css = self.get("/static/styles.css")
        self.assertEqual(status, 200)
        self.assertIn("--cp-bg: #f7f4ef", css)
        self.assertIn('font-family: "Segoe UI", Aptos, Calibri', css)

    def test_catalog_reports_the_mode(self):
        status, body = self.get("/api/catalog")
        self.assertEqual(status, 200)
        data = json.loads(body)
        self.assertIn(data["mode"], ("live", "replay"))
        self.assertTrue(data["arms"])
        self.assertTrue(data["scenarios"])

    def test_static_files_cannot_escape_the_static_folder(self):
        status, _ = self.get("/static/../server.py")
        self.assertEqual(status, 404)

    def test_unknown_route_is_a_404(self):
        status, _ = self.get("/api/nope")
        self.assertEqual(status, 404)

    def test_export_of_an_unknown_run_is_a_404(self):
        status, _ = self.get("/api/export?run_id=deadbeef")
        self.assertEqual(status, 404)

    def test_events_for_an_unknown_run_is_a_404(self):
        status, _ = self.get("/api/events?run_id=deadbeef")
        self.assertEqual(status, 404)

    def test_history_endpoint_lists_runs(self):
        status, body = self.get("/api/history")
        self.assertEqual(status, 200)
        self.assertIn("runs", json.loads(body))

    def test_an_unknown_history_run_is_a_404(self):
        status, _ = self.get("/api/history/0123456789abcdef")
        self.assertEqual(status, 404)

    def test_replay_mode_refuses_to_start_a_live_run(self):
        with patch.object(server, "endpoint_configured", return_value=False):
            status, body = self.post("/api/run", {
                "arms": [{"deployment": "gpt-5.6-luna-dz"}],
                "items": ["NM01"], "iterations": 1, "warmup": False})
        self.assertEqual(status, 400)
        self.assertIn("replay mode", body["error"])

    def test_an_invalid_plan_is_rejected_before_any_request_is_made(self):
        with patch.object(server, "server_mode", return_value="live"):
            status, body = self.post("/api/run", {
                "arms": [{"deployment": "gpt-5.6-luna-dz"}], "items": [],
                "iterations": 1, "warmup": False})
        self.assertEqual(status, 400)
        self.assertIn("at least one prompt", body["error"])

    def test_cancelling_an_unknown_run_is_harmless(self):
        status, body = self.post("/api/cancel", {"run_id": "deadbeef"})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])

    def test_text_plain_cannot_start_a_paid_run(self):
        status, body = self.post(
            "/api/run", {"items": ["NM01"]}, headers={"Content-Type": "text/plain"})
        self.assertEqual(status, 400)
        self.assertIn("application/json", body["error"])

    def test_cross_site_post_is_rejected(self):
        status, body = self.post(
            "/api/run", {"items": ["NM01"]},
            headers={"Origin": "https://malicious.example", "Host": "portal.example"})
        self.assertEqual(status, 400)
        self.assertIn("Origin", body["error"])

    def test_matching_attacker_origin_and_host_are_still_rejected(self):
        status, body = self.post(
            "/api/run", {"items": ["NM01"]},
            headers={"Origin": "http://malicious.example", "Host": "malicious.example"})
        self.assertEqual(status, 400)
        self.assertIn("Origin", body["error"])

    def test_explicit_https_portal_origin_is_accepted(self):
        with patch.object(server, "ALLOWED_ORIGINS", {"https://portal.example"}), \
                patch.object(server, "server_mode", return_value="live"):
            status, body = self.post(
                "/api/run",
                {"arms": [{"deployment": "gpt-5.6-luna-dz"}], "items": []},
                headers={"Origin": "https://portal.example", "Host": "portal.example"},
            )
        self.assertEqual(status, 400)
        self.assertIn("at least one prompt", body["error"])


class ActiveRunLimit(unittest.TestCase):
    def setUp(self):
        self.original_runs = server._runs.copy()
        server._runs.clear()

    def tearDown(self):
        server._runs.clear()
        server._runs.update(self.original_runs)

    def test_active_run_limit_rejects_additional_paid_work(self):
        server._runs["already-running"] = {"finished_at": None}
        request = {
            "dataset": "scenarios",
            "arms": [{"deployment": "gpt-5.6-luna-dz"}],
            "items": ["NM01"],
            "iterations": 1,
            "warmup": False,
        }
        with patch.object(server, "server_mode", return_value="live"), \
                patch.object(server, "MAX_ACTIVE_RUNS", 1), \
                self.assertRaisesRegex(ConsoleError, "already active"):
            server.start_run(request)


class ModeLabels(unittest.TestCase):
    def test_endpoint_host_is_partly_masked(self):
        with patch.dict("os.environ", {"AZURE_OPENAI_ENDPOINT": "https://example.openai.azure.com/"}):
            self.assertEqual(server.endpoint_label(), "exa***.openai.azure.com")
            self.assertEqual(server.server_mode(), "live")

    def test_no_endpoint_means_replay(self):
        with patch.dict("os.environ", {"AZURE_OPENAI_ENDPOINT": ""}):
            self.assertIsNone(server.endpoint_label())
            self.assertEqual(server.server_mode(), "replay")


if __name__ == "__main__":
    unittest.main(verbosity=2)
