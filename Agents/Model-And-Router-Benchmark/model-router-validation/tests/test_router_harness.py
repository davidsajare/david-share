"""Offline transport fixtures; these are not benchmark measurements."""

import sys
import io
import json
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import harness
import judge
import analyze


class RouterHarnessTests(unittest.TestCase):
    def test_effort_matrix_distinguishes_omitted_and_none(self):
        self.assertEqual(harness.parse_efforts("-,none,low"), [None, "none", "low"])
        arms = harness.build_arms(["router", "direct"], {}, False, [None, "low"])
        self.assertEqual([a["arm_id"] for a in arms], ["router", "router@low", "direct", "direct@low"])

    def test_served_model_version_suffix(self):
        self.assertEqual(harness.billing_model_name("gpt-5.6-sol-2026-07-09"), "gpt-5.6-sol")
        self.assertEqual(harness.billing_model_name("gpt-5.6-luna"), "gpt-5.6-luna")
        self.assertIsNone(harness.billing_model_name(None))

    def test_trace_and_fallback_chain(self):
        trace = {"model_router_details": {"mode": "quality", "routing_trace": [
            {"latency_ms": 20, "attempts": [
                {"model": "gpt-5.6-sol", "result": {"status": 429, "error": "limited"}},
                {"model": "gpt-5.6-luna", "result": {"status": 200}},
            ]}
        ]}}
        value = harness._extract_routing_trace(NS(model_extra={"model_selection_details": trace}))
        self.assertEqual(value["mode"], "quality")
        self.assertEqual(value["router_latency_ms"], 20)
        self.assertTrue(value["fallback"])
        self.assertEqual([a["status"] for a in value["attempts"]], [429, 200])
        self.assertIsNone(harness._extract_routing_trace(NS()))

    def stream_client(self, finish="stop"):
        chunks = [
            NS(model="gpt-5.6-luna-2026-07-09", choices=[NS(delta=NS(content="Hello "), finish_reason=None)]),
            NS(model="gpt-5.6-luna-2026-07-09", choices=[NS(delta=NS(content="there"), finish_reason=finish)]),
            NS(model="gpt-5.6-luna-2026-07-09", choices=[], usage=NS(
                prompt_tokens=100, completion_tokens=20,
                prompt_tokens_details=NS(cached_tokens=25),
                completion_tokens_details=NS(reasoning_tokens=10),
            ), model_extra={"model_selection_details": {"model_router_details": {
                "mode": "cost", "routing_trace": [
                    {"latency_ms": 20, "attempts": [{"model": "gpt-5.6-luna", "result": {"status": 200}}]}
                ]
            }}}),
        ]
        create = Mock(return_value=iter(chunks))
        return NS(chat=NS(completions=NS(create=create))), create

    def test_stream_usage_only_final_chunk_and_timing(self):
        client, create = self.stream_client()
        with patch.object(harness.time, "perf_counter", side_effect=[0, 1, 2, 2.1]):
            result = harness._run_one_chat(client, "router", {"text": "test"}, 9000, None)
        kwargs = create.call_args.kwargs
        self.assertNotIn("tools", kwargs)
        self.assertNotIn("reasoning_effort", kwargs)
        self.assertEqual(kwargs["messages"][0]["content"], harness.SYSTEM_MSG)
        self.assertEqual(kwargs["extra_headers"], harness.ROUTER_TRACE_HEADER)
        self.assertEqual((result["ttft_ms"], result["e2e_ms"], result["decode_tps"]), (1000, 2100, 9))
        self.assertEqual((result["prompt_tokens"], result["completion_tokens"], result["cached_tokens"], result["reasoning_tokens"]), (100, 20, 25, 10))
        self.assertEqual(result["response_text"], "Hello there")
        self.assertEqual(result["routing"]["mode"], "cost")
        self.assertFalse(result["routing"]["fallback"])
        self.assertEqual(result["finish_reason"], "stop")

    def test_filtered_and_unterminated_streams_are_not_completed(self):
        for finish in ("content_filter", None, "tool_calls"):
            with self.subTest(finish=finish):
                client, _ = self.stream_client(finish)
                with patch.object(harness.time, "perf_counter", side_effect=[0, 1, 2, 2.1]):
                    result = harness._run_one_chat(client, "router", {"text": "test"}, 9000, None)
                self.assertNotEqual(result["status"], "completed")
                self.assertEqual(result["finish_reason"], finish)
                self.assertTrue(result["error"])
                self.assertEqual(result["incomplete_reason"], finish or "missing_finish_reason")

    def test_judge_skips_incomplete_earlier_answers(self):
        complete = {"arm": "router", "question_id": "Q1", "api": "chat", "iteration": 3,
                    "status": "completed", "truncated": False, "response_text": "answer",
                    "error": None, "warmup": False, "finish_reason": "stop"}
        for changes in (
            {"status": "incomplete", "truncated": True, "finish_reason": "length"},
            {"finish_reason": "content_filter"},
            {"finish_reason": None},
            {"error": "transport failure"},
            {"response_text": " "},
        ):
            with self.subTest(changes=changes):
                early = {**complete, "iteration": 2, **changes}
                self.assertEqual(judge.select_for_scoring([early, complete]), [complete])
        legacy = {k: v for k, v in complete.items() if k != "finish_reason"}
        self.assertEqual(judge.select_for_scoring([legacy]), [legacy])

    def test_router_cli_discloses_latency_and_decode_limits(self):
        root = Path(__file__).resolve().parents[1]
        rows = analyze.load_records(root / "outputs" / "router_20260909_223737.metrics.jsonl")
        pricing = json.loads((root / "config" / "pricing.json").read_text(encoding="utf-8"))["models"]
        output = io.StringIO()
        with redirect_stdout(output):
            analyze.report_router(rows, pricing)
        text = output.getvalue()
        self.assertIn("Paired TTFT differences", text)
        self.assertIn("Global/DataZone", text)
        self.assertIn("fixed serial", text)
        self.assertIn("NOT model/GPU generation throughput", text)
        self.assertNotIn("5. Router overhead", text)

    def test_length_finish_and_effort_forwarding(self):
        client, create = self.stream_client("length")
        with patch.object(harness.time, "perf_counter", side_effect=[0, 1, 2, 2.1]):
            result = harness._run_one_chat(client, "router", {"text": "test"}, 9000, "low")
        self.assertEqual(create.call_args.kwargs["reasoning_effort"], "low")
        self.assertTrue(result["truncated"])
        self.assertEqual(result["status"], "incomplete")

    def test_transport_error_is_retained(self):
        client = NS(chat=NS(completions=NS(create=Mock(side_effect=RuntimeError("test transport failure")))))
        result = harness._run_one_chat(client, "router", {"text": "test"}, 9000, None)
        self.assertIn("test transport failure", result["error"])
        self.assertIsNone(result["status"])
        self.assertIsNone(result["ttft_ms"])

    def test_missing_usage_is_not_a_completed_answer(self):
        chunks = [NS(model="gpt-5.6-luna-2026-07-09", choices=[NS(delta=NS(content="answer"), finish_reason="stop")], usage=None)]
        client = NS(chat=NS(completions=NS(create=Mock(return_value=iter(chunks)))))
        with patch.object(harness.time, "perf_counter", side_effect=[0, 1, 2]):
            result = harness._run_one_chat(client, "router", {"text": "test"}, 9000, None)
        self.assertIsNone(result["status"])
        self.assertEqual(result["incomplete_reason"], "missing_usage")
        self.assertTrue(result["error"])
        self.assertEqual(judge.select_for_scoring([{**result, "arm": "r", "question_id": "Q", "iteration": 2, "warmup": False, "api": "chat"}]), [])
        # Responses path: a completed event without usage is equally not billable.
        event = NS(type="response.completed", response=NS(usage=None, model="gpt-5.6-luna", status="completed", incomplete_details=None))
        rclient = NS(responses=NS(create=Mock(return_value=iter([NS(type="response.output_text.delta", delta="hi"), event]))))
        with patch.object(harness.time, "perf_counter", side_effect=[0, 1, 2]):
            rresult = harness.run_one(rclient, "direct", {"text": "test"}, 9000, None, api="responses")
        self.assertEqual(rresult["status"], "incomplete")
        self.assertEqual(rresult["incomplete_reason"], "missing_usage")
        self.assertTrue(rresult["error"])

    def test_client_disables_silent_sdk_retries(self):
        with patch.dict(harness.os.environ, {"AZURE_OPENAI_ENDPOINT": "https://example.invalid", "AZURE_OPENAI_API_KEY": "k"}):
            client, _ = harness.build_client()
        self.assertEqual(client.max_retries, 0)

    def test_router_analysis_excludes_invalid_and_fails_closed(self):
        root = Path(__file__).resolve().parents[1]
        rows = analyze.load_records(root / "outputs" / "router_20260909_223737.metrics.jsonl")
        pricing = json.loads((root / "config" / "pricing.json").read_text(encoding="utf-8"))["models"]
        base = [r for r in rows if not r["warmup"] and r["arm"] == "router-sol-luna-cost"][:6]
        truncated = {**base[0], "status": "incomplete", "truncated": True, "finish_reason": "length"}
        no_usage = {**base[1], "prompt_tokens": 0, "completion_tokens": 0}
        output = io.StringIO()
        with redirect_stdout(output):
            analyze.report_router([truncated, no_usage, *base[2:]], pricing)
        self.assertIn("6 measured requests, 2 excluded", output.getvalue())
        mixed = [dict(base[0]), {**base[1], "region": "eastus"}]
        with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
            analyze.report_router(mixed, pricing)


if __name__ == "__main__":
    unittest.main()
