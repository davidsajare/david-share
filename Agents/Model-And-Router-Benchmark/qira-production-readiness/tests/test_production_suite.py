"""Offline plumbing tests for the production-readiness suite: fake transports, real logic."""

import io
import json
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fallback_client  # noqa: E402
import harness  # noqa: E402
import resilience_test  # noqa: E402
import sessions  # noqa: E402
import sustained_load  # noqa: E402
from openai import APIConnectionError, APIError, APIStatusError, RateLimitError  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def chat_stream(text="answer", usage=True, model="gpt-5.6-luna-2026-07-09"):
    chunks = [NS(model=model, choices=[NS(delta=NS(content=text), finish_reason="stop")], usage=None)]
    if usage:
        chunks.append(NS(model=model, choices=[], usage=NS(prompt_tokens=100, completion_tokens=30,
                                                            prompt_tokens_details=NS(cached_tokens=40),
                                                            completion_tokens_details=NS(reasoning_tokens=0))))
    return iter(chunks)


def responses_stream(text="answer", model="gpt-5.6-luna"):
    return iter([NS(type="response.output_text.delta", delta=text),
                 NS(type="response.completed", response=NS(model=model, status="completed", incomplete_details=None,
                                                             usage=NS(input_tokens=100, output_tokens=30,
                                                                      input_tokens_details=NS(cached_tokens=40),
                                                                      output_tokens_details=NS(reasoning_tokens=0))))])


def chat_stream_factory():
    return chat_stream()


def http_error(cls, status, headers=None):
    response = NS(status_code=status, headers=headers or {}, request=None)
    if cls is RateLimitError:
        return RateLimitError("429", response=response, body=None)
    return APIStatusError("err", response=response, body=None)


class HarnessHistoryTests(unittest.TestCase):
    def test_history_is_inserted_between_system_and_user_on_both_surfaces(self):
        history = [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}]
        create = Mock(return_value=chat_stream())
        with patch.object(harness.time, "perf_counter", side_effect=[0, 1, 2, 2.1]):
            harness.run_one(NS(chat=NS(completions=NS(create=create))), "d", {"text": "q2"}, 9000, None, api="chat", history=history)
        msgs = create.call_args.kwargs["messages"]
        self.assertEqual([m["role"] for m in msgs], ["system", "user", "assistant", "user"])
        self.assertEqual(msgs[-1]["content"], "q2")
        rcreate = Mock(return_value=responses_stream())
        with patch.object(harness.time, "perf_counter", side_effect=[0, 1, 2, 2.1]):
            harness.run_one(NS(responses=NS(create=rcreate)), "d", {"text": "q2"}, 9000, None, api="responses", history=history)
        inp = rcreate.call_args.kwargs["input"]
        self.assertEqual([m["role"] for m in inp], ["system", "user", "assistant", "user"])
        self.assertEqual(inp[0]["content"], harness.SYSTEM_MSG)


class FallbackClientTests(unittest.TestCase):
    def make(self, primary_effects, fallback_effects, policy="reactive", headers=None):
        calls = []

        def create(**kwargs):
            calls.append(kwargs["model"])
            effects = primary_effects if kwargs["model"] == "primary" else fallback_effects
            effect = effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return effect()

        def raw_create(**kwargs):
            calls.append(kwargs["model"])
            effect = primary_effects.pop(0)
            if isinstance(effect, Exception):
                raise effect
            return NS(headers=headers or {}, parse=effect)

        completions = NS(create=create, with_raw_response=NS(create=raw_create))
        base = NS(chat=NS(completions=completions), responses=NS(create=create, with_raw_response=NS(create=raw_create)), max_retries=0)
        return fallback_client.FallbackClient(base, ["primary", "secondary"], policy), calls

    def test_reactive_falls_back_on_429_and_records_retry_after(self):
        fc, calls = self.make([http_error(RateLimitError, 429, {"retry-after": "7"})], [chat_stream])
        events = list(fc.chat.completions.create(model="ignored", messages=[]))
        self.assertEqual(calls, ["primary", "secondary"])
        self.assertTrue(events)
        d = fallback_client.last_decision()
        self.assertEqual(d.served_by, "secondary")
        self.assertEqual([a["status"] for a in d.attempts], [429, 200])
        self.assertEqual(d.attempts[0]["retry_after_s"], 7.0)
        self.assertEqual(d.attempts[0]["phase"], "request")
        self.assertTrue(d.attempts[0]["rate_limit"])
        self.assertGreaterEqual(d.fallback_overhead_ms, 0)
        self.assertEqual(fc.utilization, 1.0)

    def test_in_stream_rate_limit_after_http_200_falls_back_before_content(self):
        # Observed on Azure OpenAI Responses streaming (2026-09-11): HTTP 200, then an error event
        # "Your requests to gpt-5.6-luna for gpt-5.6-luna-lowcap in swedencentral have exceeded rate limit."
        def failing_stream():
            yield NS(type="response.created")
            raise APIError("Your requests to gpt-5.6-luna for gpt-5.6-luna-lowcap in swedencentral have exceeded rate limit.", request=NS(), body=None)
        fc, calls = self.make([failing_stream], [lambda: responses_stream("from secondary")])
        events = list(fc.responses.create(model="x", input=[]))
        self.assertEqual(calls, ["primary", "secondary"])
        self.assertEqual([e.type for e in events][-2:], ["response.output_text.delta", "response.completed"])
        d = fallback_client.last_decision()
        self.assertEqual(d.served_by, "secondary")
        self.assertEqual(d.in_stream_fallbacks, 1)
        self.assertEqual(d.attempts[0]["phase"], "stream")
        self.assertTrue(d.attempts[0]["rate_limit"])
        self.assertEqual(d.attempts[1]["status"], 200)
        self.assertEqual(fc.utilization, 1.0)

    def test_error_after_content_is_not_retried(self):
        def failing_after_content():
            yield NS(type="response.output_text.delta", delta="partial ")
            raise APIError("upstream reset", request=NS(), body=None)
        fc, calls = self.make([failing_after_content], [responses_stream])
        stream = fc.responses.create(model="x", input=[])
        first = next(stream)
        self.assertEqual(first.delta, "partial ")
        with self.assertRaises(APIError):
            list(stream)
        self.assertEqual(calls, ["primary"])
        self.assertEqual(fallback_client.last_decision().in_stream_fallbacks, 0)

    def test_reactive_falls_back_on_5xx_and_connection_but_not_4xx(self):
        fc, calls = self.make([http_error(APIStatusError, 503)], [chat_stream])
        list(fc.chat.completions.create(model="x", messages=[]))
        self.assertEqual(fallback_client.last_decision().served_by, "secondary")
        fc, calls = self.make([APIConnectionError(request=NS())], [chat_stream])
        list(fc.chat.completions.create(model="x", messages=[]))
        self.assertEqual(fallback_client.last_decision().served_by, "secondary")
        fc, calls = self.make([http_error(APIStatusError, 400)], [chat_stream])
        with self.assertRaises(APIStatusError):
            list(fc.chat.completions.create(model="x", messages=[]))
        self.assertEqual(calls, ["primary"])
        self.assertTrue(fallback_client.last_decision().exhausted)

    def test_none_policy_never_falls_back(self):
        fc, calls = self.make([http_error(RateLimitError, 429)], [chat_stream], policy="none")
        with self.assertRaises(RateLimitError):
            list(fc.chat.completions.create(model="x", messages=[]))
        self.assertEqual(calls, ["primary"])

    def test_proactive_reads_headers_then_skips_primary_above_threshold(self):
        headers = {"x-ratelimit-remaining-requests": "1", "x-ratelimit-limit-requests": "10",
                   "x-ratelimit-remaining-tokens": "9000", "x-ratelimit-limit-tokens": "10000"}
        fc, calls = self.make([chat_stream, chat_stream], [chat_stream, chat_stream], policy="proactive", headers=headers)
        list(fc.chat.completions.create(model="x", messages=[]))
        self.assertEqual(fallback_client.last_decision().served_by, "primary")
        self.assertAlmostEqual(fc.utilization, 0.9)  # worst of requests 90 % / tokens 10 %
        self.assertEqual(fallback_client.last_decision().attempts[0]["ratelimit_headers"], headers)
        list(fc.chat.completions.create(model="x", messages=[]))
        d = fallback_client.last_decision()
        self.assertTrue(d.proactive_skip)
        self.assertEqual(d.served_by, "secondary")
        self.assertEqual(calls, ["primary", "secondary"])

    def test_proactive_utilization_expires_like_apim_cache(self):
        headers = {"x-ratelimit-remaining-requests": "0", "x-ratelimit-limit-requests": "10"}
        fc, calls = self.make([chat_stream, chat_stream], [chat_stream], policy="proactive", headers=headers)
        fc.utilization_ttl_s = 0.05
        list(fc.chat.completions.create(model="x", messages=[]))
        self.assertEqual(fc.utilization, 1.0)
        time.sleep(0.08)
        self.assertEqual(fc.utilization, 0.0)  # expired → primary is probed again
        list(fc.chat.completions.create(model="x", messages=[]))
        self.assertEqual(calls, ["primary", "primary"])
        self.assertFalse(fallback_client.last_decision().proactive_skip)

    def test_exhausted_chain_raises_last_error(self):
        fc, calls = self.make([http_error(RateLimitError, 429)], [http_error(RateLimitError, 429)])
        with self.assertRaises(RateLimitError):
            list(fc.chat.completions.create(model="x", messages=[]))
        self.assertEqual(calls, ["primary", "secondary"])
        self.assertTrue(fallback_client.last_decision().exhausted)

    def test_harness_records_in_stream_rate_limit_as_error_when_no_fallback(self):
        def failing_stream():
            yield NS(type="response.created")
            raise APIError("requests have exceeded rate limit.", request=NS(), body=None)
        fc, calls = self.make([failing_stream], [responses_stream], policy="none")
        with patch.object(harness.time, "perf_counter", side_effect=[0, 1, 2, 2.1]):
            result = harness.run_one(fc, "chain", {"text": "q"}, 9000, None, api="responses")
        self.assertIn("exceeded rate limit", result["error"])
        self.assertEqual(sustained_load.classify_error(result["error"]), "429")
        self.assertIsNone(result["status"])


def fake_run_one(client, deployment, item, cap, effort, api="responses", history=None):
    time.sleep(0.02)
    fail = item["id"].endswith("02") or item["id"].endswith("T2")
    return {"ttft_ms": 100.0, "e2e_ms": 150.0, "decode_ms": 50.0, "tpot_ms": 5.0, "decode_tps": 200.0,
            "prompt_tokens": 0 if fail else 100 + 50 * len(history or []), "cached_tokens": 0 if fail else 20 * len(history or []),
            "completion_tokens": 0 if fail else 30, "reasoning_tokens": 0, "status": None if fail else "completed",
            "truncated": False, "incomplete_reason": None, "finish_reason": None if fail else "stop",
            "model_actually_served": "gpt-5.6-luna", "routing": None, "response_text": "" if fail else f"answer {item['id']}",
            "error": "RateLimitError: Error code: 429" if fail else None}


class RunnerTests(unittest.TestCase):
    def run_module(self, module, argv):
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            with patch.object(module, "OUTPUT_DIR", Path(tmp)), patch.object(sys, "argv", argv), \
                 patch.object(harness, "build_client", return_value=(NS(max_retries=0), "https://x.example")), \
                 patch.object(harness, "measure_rtt", return_value=1.0), patch.object(harness, "run_one", fake_run_one):
                self.assertEqual(module.main(), 0)
            records = [json.loads(l) for p in Path(tmp).glob("*.jsonl") for l in p.read_text(encoding="utf-8").splitlines()]
            summaries = [json.loads(p.read_text(encoding="utf-8")) for p in Path(tmp).glob("*.summary.json")]
        return records, summaries

    def test_sessions_replay_history_and_stop_after_a_broken_turn(self):
        records, _ = self.run_module(sessions, ["sessions.py", "--dataset", str(ROOT / "datasets" / "qira_sessions.jsonl"),
                                                "--arms", "gpt-5.6-luna@none,router-sol-luna-balanced", "--iterations", "2", "--region", "swedencentral"])
        # T2 fails in the fake transport, so each replay records exactly turns 1 and 2.
        self.assertEqual(len(records), 2 * 6 * 2 * 2)
        by_turn = {r["turn"] for r in records}
        self.assertEqual(by_turn, {1, 2})
        t2 = [r for r in records if r["turn"] == 2]
        self.assertTrue(all(r["history_messages"] == 2 and r["error"] for r in t2))
        t1 = [r for r in records if r["turn"] == 1]
        self.assertTrue(all(r["history_messages"] == 0 and r["prompt_tokens"] == 100 for r in t1))
        self.assertTrue(all(r["api"] == "chat" for r in records if r["arm"] == "router-sol-luna-balanced"))
        self.assertTrue(all(r["tools_enabled"] is False for r in records))

    def test_sustained_counts_only_window_and_keeps_level_in_flight(self):
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
        with patch.object(harness, "run_one", tracking):
            with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
                argv = ["sustained_load.py", "--dataset", str(ROOT / "datasets" / "qira_scenarios.jsonl"), "--arms", "gpt-5.6-luna@none",
                        "--levels", "4", "--duration", "1.0", "--ramp", "0.3", "--settle-seconds", "0", "--region", "swedencentral"]
                with patch.object(sustained_load, "OUTPUT_DIR", Path(tmp)), patch.object(sys, "argv", argv), \
                     patch.object(harness, "build_client", return_value=(NS(max_retries=0), "https://x.example")), \
                     patch.object(harness, "measure_rtt", return_value=1.0), patch.object(harness, "run_one", tracking):
                    self.assertEqual(sustained_load.main(), 0)
                records = [json.loads(l) for p in Path(tmp).glob("sustained_*.jsonl") for l in p.read_text(encoding="utf-8").splitlines()]
                summary = json.loads(next(Path(tmp).glob("*.summary.json")).read_text(encoding="utf-8"))["summaries"][0]
        self.assertEqual(peak[0], 4)
        self.assertGreater(len(records), 20)
        self.assertTrue(all(r["counted"] == (not r["in_ramp"] and not r["cut_by_deadline"]) for r in records))
        self.assertEqual(summary["counted"], sum(r["counted"] for r in records))
        self.assertEqual(summary["ok"] + summary["errors"], summary["counted"])
        self.assertAlmostEqual(summary["aggregate_output_tok_per_s"] * summary["window_s"], 30 * summary["ok"], delta=30 * summary["ok"] * 0.05 + 1)
        self.assertEqual(summary["errors_429"], summary["errors"])

    def test_resilience_attaches_decisions_and_summaries(self):
        def fake_build(chain, policy, threshold):
            base = NS(chat=NS(completions=NS(create=Mock())), responses=NS(create=Mock()), max_retries=0)
            return fallback_client.FallbackClient(base, chain, policy, threshold), "https://x.example"

        def fake_run(fc, deployment, item, cap, effort, api="responses", history=None):
            fallback_client._STATE.decision = fallback_client.Decision(chain=fc.chain, policy=fc.policy, served_by=fc.chain[1] if fc.policy != "none" else None,
                                                                        attempts=[{"deployment": fc.chain[0], "position": 0, "status": 429, "retry_after_s": 3.0},
                                                                                  {"deployment": fc.chain[1], "position": 1, "status": 200}] if fc.policy != "none"
                                                                        else [{"deployment": fc.chain[0], "position": 0, "status": 429, "retry_after_s": 3.0}],
                                                                        fallback_overhead_ms=12.0, exhausted=fc.policy == "none")
            m = fake_run_one(fc, deployment, item, cap, effort, api, history)
            if fc.policy != "none":
                m.update(error=None, status="completed", prompt_tokens=100, completion_tokens=30, response_text="ok", finish_reason="stop")
            else:
                m.update(error="RateLimitError: 429", status=None, prompt_tokens=0, completion_tokens=0, response_text="")
            return m
        with tempfile.TemporaryDirectory() as tmp, redirect_stdout(io.StringIO()):
            argv = ["resilience_test.py", "--dataset", str(ROOT / "datasets" / "qira_scenarios.jsonl"), "--primary", "small", "--fallback", "big",
                    "--policies", "none,reactive", "--level", "2", "--duration", "0.6", "--ramp", "0.1", "--settle-seconds", "0", "--region", "swedencentral"]
            with patch.object(resilience_test, "OUTPUT_DIR", Path(tmp)), patch.object(sys, "argv", argv), \
                 patch.object(fallback_client, "build_fallback_client", fake_build), patch.object(harness, "measure_rtt", return_value=1.0), \
                 patch.object(harness, "run_one", fake_run):
                self.assertEqual(resilience_test.main(), 0)
            summary = json.loads(next(Path(tmp).glob("*.summary.json")).read_text(encoding="utf-8"))
        none, reactive = summary["summaries"]
        self.assertEqual(none["policy"], "none")
        self.assertEqual(none["ok"], 0)
        self.assertEqual(none["errors_429"], none["counted"])
        self.assertEqual(reactive["success_rate"], 1.0)
        self.assertEqual(reactive["fallback_share_of_ok"], 1.0)
        self.assertEqual(reactive["primary_429_seen"], reactive["counted"])
        self.assertEqual(reactive["retry_after_s_p50"], 3.0)
        self.assertEqual(reactive["fallback_overhead_ms_p50"], 12.0)


if __name__ == "__main__":
    unittest.main()
