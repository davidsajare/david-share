#!/usr/bin/env python3
"""Closed-loop throughput client for a vLLM OpenAI-compatible server.

Sends the same prompts at a fixed concurrency, greedy, and measures wall-clock
tokens/s from the server's own ``usage.completion_tokens``. Speculative-decoding
acceptance is not visible to the client; it is read from the server log separately.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import statistics
import time
import urllib.request
from pathlib import Path


def load_prompts(path: Path, limit: int | None, skip: int = 0):
    records = []
    seen = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        seen += 1
        if seen <= skip:
            continue
        records.append(json.loads(line))
        if limit and len(records) >= limit:
            break
    return records


def one_request(base_url, model, prompt, max_tokens, timeout, thinking=False):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "top_p": 1.0,
        "seed": 20260909,
        "chat_template_kwargs": {"enable_thinking": thinking},
    }).encode("utf-8")
    request = urllib.request.Request(f"{base_url}/v1/chat/completions", data=body,
                                     headers={"Content-Type": "application/json"})
    started = time.perf_counter()
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    seconds = time.perf_counter() - started
    message = payload["choices"][0]["message"]
    content = message.get("content") or ""
    reasoning = message.get("reasoning_content")
    # Non-thinking responses carry no reasoning_content, so their hash stays identical to Round 5.
    text = f"{reasoning}\x1e{content}" if reasoning else content
    return {
        "completion_tokens": payload["usage"]["completion_tokens"],
        "prompt_tokens": payload["usage"]["prompt_tokens"],
        "seconds": round(seconds, 4),
        "finish_reason": payload["choices"][0]["finish_reason"],
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "text_head": content[:120],
        "reasoning_chars": len(reasoning) if reasoning else 0,
    }


def run_level(base_url, model, prompts, concurrency, max_tokens, timeout, thinking=False):
    started = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        rows = list(pool.map(lambda p: one_request(base_url, model, p, max_tokens, timeout, thinking), prompts))
    wall = time.perf_counter() - started
    tokens = sum(r["completion_tokens"] for r in rows)
    return {
        "concurrency": concurrency,
        "requests": len(rows),
        "completion_tokens": tokens,
        "wall_seconds": round(wall, 2),
        "tokens_per_second": round(tokens / wall, 2),
        "median_request_seconds": round(statistics.median(r["seconds"] for r in rows), 3),
        "finish_length": sum(1 for r in rows if r["finish_reason"] == "length"),
        "per_request": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18080")
    parser.add_argument("--model", default="Qwen/Qwen3.8-27B")
    parser.add_argument("--prompts", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--skip", type=int, default=0, help="skip the first N prompt lines")
    parser.add_argument("--thinking", action="store_true", help="enable_thinking=true in chat_template_kwargs")
    parser.add_argument("--max-tokens", type=int, default=256)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[1, 4])
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()

    records = load_prompts(Path(args.prompts), args.limit, args.skip)
    prompts = [r["prompt"] for r in records]

    for prompt in prompts[: args.warmup]:
        one_request(args.base_url, args.model, prompt, 32, args.timeout, args.thinking)

    levels = []
    for concurrency in args.concurrency:
        level = run_level(args.base_url, args.model, prompts, concurrency, args.max_tokens, args.timeout, args.thinking)
        levels.append(level)
        print(json.dumps({k: v for k, v in level.items() if k != "per_request"}), flush=True)

    payload = {"label": args.label, "base_url": args.base_url, "model": args.model,
               "prompts": args.prompts, "skip": args.skip, "num_prompts": len(prompts),
               "max_tokens": args.max_tokens, "thinking": args.thinking,
               "decoding": f"temperature 0.0 via OpenAI chat completions, enable_thinking={'true' if args.thinking else 'false'}",
               "levels": levels}
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print("VLLM_CLIENT_BENCH=PASS", flush=True)


if __name__ == "__main__":
    main()
