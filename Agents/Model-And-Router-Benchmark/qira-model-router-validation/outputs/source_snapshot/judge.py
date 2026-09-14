#!/usr/bin/env python3
"""
Quality scoring for Task A — blind LLM-as-a-Judge.

The meeting brief lists 质量 (quality) as the first evaluation dimension, but
quality is the one dimension the harness cannot measure directly. This script
scores the recorded responses offline, so it never touches the latency path.

Method follows david-share/Agents/LLM-Judgment:
  - Single Evaluation (reference-free) — the Qira scenarios have no gold answer
  - Explicit, narrow criteria rather than one vague "is this good" question
  - Fixed scoring scale with a short justification, which improves consistency

Two bias controls that matter more than the rubric itself:

  1. BLIND. The judge never sees which model produced a response. Judges are
     known to favour outputs they recognise as their own family, and the whole
     point here is to rank sibling models against each other.
  2. DEDUPLICATED. Only one iteration per (model, question) is scored. Scoring
     every repeat inflates n without adding information, since the harness runs
     the same prompt repeatedly for latency stability, not output diversity.

Usage:
  python judge.py outputs/direct_<ts>.jsonl --judge-deployment <deployment> \\
      --region eastus2

  python analyze.py outputs/direct_<ts>.jsonl --quality outputs/quality_<ts>.jsonl

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "outputs"

API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2025-04-01-preview")

DIMENSIONS = {
    "instruction_following": "Did the response do exactly what was asked, including any explicit constraints on length, format, or output shape?",
    "accuracy": "Is the response factually and logically correct given only the information in the request? Penalise anything invented that was not in the prompt.",
    "completeness": "Did the response cover every element the request required, without padding?",
    "usefulness": "Would this response actually be usable as-is by the person who asked, without rework?",
    "tone_fit": "Does the register match what the request called for (spoken and brief, executive and terse, formal document prose, and so on)?",
}

JUDGE_SYSTEM = """You are a strict evaluator of AI assistant responses.

You will be given a REQUEST and a RESPONSE. Score the RESPONSE on each criterion using this scale:

5 = excellent, no meaningful fault
4 = good, minor fault that would not require rework
3 = acceptable, needs light editing
2 = poor, needs substantial rework
1 = unusable for the stated purpose

Rules:
- Judge only the RESPONSE against the REQUEST. Do not reward length.
- If the request set an explicit constraint (a word limit, "reply with only a number",
  "output only the prompt", a required JSON shape), violating it caps
  instruction_following at 2.
- Do not speculate about which system produced the response.
- Return ONLY a JSON object, no prose, no code fences.

Return exactly this shape:
{"instruction_following": <1-5>, "accuracy": <1-5>, "completeness": <1-5>,
 "usefulness": <1-5>, "tone_fit": <1-5>, "justification": "<one sentence>"}"""


def load_records(path: Path) -> list[dict]:
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                records.append(json.loads(line))
    if not records:
        raise SystemExit(f"{path} contained no records.")
    return records


def load_dataset(path: Path) -> dict[str, dict]:
    items = {}
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                item = json.loads(line)
                items[item["id"]] = item
    return items


def select_for_scoring(records: list[dict]) -> list[dict]:
    """One representative response per (arm, question). Warmups and errors out."""
    best: dict[tuple, dict] = {}
    for r in records:
        if r.get("warmup") or r.get("error"):
            continue
        text = r.get("response_text") or ""
        if not text.strip():
            continue
        arm = r.get("arm") or r.get("model_requested")
        key = (arm, r.get("question_id"))
        # Prefer the earliest measured iteration for determinism.
        if key not in best or r.get("iteration", 0) < best[key].get("iteration", 0):
            best[key] = r
    return list(best.values())


def _parse_scores(raw: str) -> dict | None:
    """Judges occasionally wrap JSON in fences or add a stray sentence."""
    if not raw:
        return None
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?|```$", "", cleaned, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not all(d in data for d in DIMENSIONS):
        return None
    for d in DIMENSIONS:
        value = data[d]
        if type(value) is not int or not 1 <= value <= 5:
            return None
        data[d] = value
    return data


def judge_one(client, deployment: str, request_text: str, response_text: str,
              max_chars: int) -> tuple[dict | None, str | None]:
    if max_chars and max(len(request_text), len(response_text)) > max_chars:
        return None, "Text exceeds --max-chars; refusing to score a truncated response."
    criteria = "\n".join(f"- {k}: {v}" for k, v in DIMENSIONS.items())
    user = (
        f"CRITERIA\n{criteria}\n\n"
        f"REQUEST\n{request_text}\n\n"
        f"RESPONSE\n{response_text}"
    )
    from openai import APIError
    try:
        result = client.responses.create(
            model=deployment,
            input=[
                {"role": "system", "content": JUDGE_SYSTEM},
                {"role": "user", "content": user},
            ],
            max_output_tokens=4096,
        )
        if result.status != "completed":
            return None, f"Judge response not completed: {result.status}"
        raw = getattr(result, "output_text", None)
        if raw is None:
            parts = []
            for item in getattr(result, "output", []) or []:
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "text", None):
                        parts.append(c.text)
            raw = "".join(parts)
        scores = _parse_scores(raw)
        if scores is None:
            return None, f"unparseable judge output: {(raw or '')[:120]}"
        return scores, None
    except APIError as exc:
        return None, f"{type(exc).__name__}: {exc}"


def build_client():
    """Reuse the harness client so Entra ID / key fallback behave identically."""
    import harness
    client, _endpoint = harness.build_client()
    return client


def main() -> int:
    p = argparse.ArgumentParser(description="Blind LLM-as-a-Judge quality scoring for Task A results.")
    p.add_argument("results", help="path to an outputs/*.jsonl produced by harness.py")
    p.add_argument("--judge-deployment", required=True, dest="judge_deployment",
                   help="deployment name of the judge model")
    p.add_argument("--dataset", default=str(ROOT / "datasets" / "qira_scenarios.jsonl"),
                   help="dataset the run used, for the original request text")
    p.add_argument("--out", default=None, help="output path (default outputs/quality_<run_id>.jsonl)")
    p.add_argument("--max-chars", type=int, default=0, dest="max_chars",
                   help="reject longer text rather than truncate it (0 = no character limit)")
    p.add_argument("--sleep", type=float, default=0.0)
    p.add_argument("--dry-run", action="store_true", dest="dry_run",
                   help="report what would be scored and exit without calling the judge")
    args = p.parse_args()

    records = load_records(Path(args.results))
    dataset = load_dataset(Path(args.dataset))
    targets = select_for_scoring(records)

    if not targets:
        raise SystemExit(
            "Nothing to score. Either every record errored, or this file predates "
            "response_text being stored — re-run harness.py to capture full responses."
        )

    missing_prompt = [t["question_id"] for t in targets if t["question_id"] not in dataset]
    if missing_prompt:
        raise SystemExit(
            f"--dataset does not contain: {sorted(set(missing_prompt))}\n"
            "Point --dataset at the dataset this run actually used."
        )

    models = sorted({t.get("arm") or t["model_requested"] for t in targets})
    questions = sorted({t["question_id"] for t in targets})
    print(f"scoring {len(targets)} responses  ({len(models)} arms × {len(questions)} questions)")
    print(f"judge: {args.judge_deployment}")
    print("blind: the judge never sees which model produced a response")

    deployments = {t["model_requested"] for t in targets}
    if args.judge_deployment in deployments:
        print(f"\n[warn] The judge ({args.judge_deployment}) is also one of the models under test.")
        print("       Self-preference bias is possible. Prefer a judge outside the candidate set.")

    if args.dry_run:
        print("\nWould score:")
        for m in models:
            n = sum(1 for t in targets if (t.get("arm") or t["model_requested"]) == m)
            print(f"  {m:<26} {n} responses")
        print("\nDry run, no judge calls made.")
        return 0

    client = build_client()

    # Shuffle so the judge does not see all of one model's work consecutively.
    random.seed(0)
    random.shuffle(targets)

    run_id = records[0].get("run_id", datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S"))
    out_path = Path(args.out) if args.out else OUTPUT_DIR / f"quality_{run_id}.jsonl"
    out_path.parent.mkdir(parents=True, exist_ok=True)

    scored = failed = 0
    with out_path.open("w", encoding="utf-8") as fh:
        for i, rec in enumerate(targets, 1):
            item = dataset[rec["question_id"]]
            scores, err = judge_one(client, args.judge_deployment,
                                    item["text"], rec["response_text"], args.max_chars)
            row = {
                "run_id": run_id,
                "question_id": rec["question_id"],
                "scenario": rec.get("scenario"),
                "arm": rec.get("arm") or rec.get("model_requested"),
                "model_requested": rec["model_requested"],
                "reasoning_effort": rec.get("reasoning_effort"),
                "model_actually_served": rec.get("model_actually_served"),
                "iteration": rec.get("iteration"),
                "judge_deployment": args.judge_deployment,
                "error": err,
            }
            if scores:
                row.update({d: scores[d] for d in DIMENSIONS})
                row["quality_mean"] = round(sum(scores[d] for d in DIMENSIONS) / len(DIMENSIONS), 3)
                row["justification"] = str(scores.get("justification", ""))[:300]
                scored += 1
            else:
                failed += 1
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            fh.flush()

            mark = f"{row.get('quality_mean'):.2f}" if scores else "FAIL"
            print(f"  [{i}/{len(targets)}] {rec['question_id']:6s} {row['arm']:<26} {mark}"
                  + (f"  {err[:60]}" if err else ""))
            if args.sleep:
                time.sleep(args.sleep)

    print(f"\nscored {scored}, failed {failed} → {out_path}")
    if failed:
        print("Failed rows are kept with error set so the sample size stays auditable.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
