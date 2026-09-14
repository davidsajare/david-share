"""Re-score every WFM01 arm on full text, keeping the original scores intact.

One-off repair for run 20260909_120534 (paths are fixed on purpose); it calls
the judge deployment and needs the same credentials as judge.py.

Author: Xinyu Wei (魏新宇)
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import judge


def main():
    results = ROOT / "outputs" / "direct_20260909_120534.jsonl"
    original = ROOT / "outputs" / "quality_20260909_120534.jsonl"
    destination = ROOT / "outputs" / "quality_fulltext_20260909_120534.jsonl"
    records = judge.load_records(results)
    scores = judge.load_records(original)
    dataset = judge.load_dataset(ROOT / "datasets" / "qira_scenarios.jsonl")
    targets = [r for r in judge.select_for_scoring(records) if r["question_id"] == "WFM01"]
    if len(targets) != 11 or len(scores) != 187:
        raise ValueError("Unexpected matrix size; refusing a partial repair.")
    replacements = {}
    client = judge.build_client()
    for record in targets:
        values, error = judge.judge_one(
            client, "judge-terra", dataset["WFM01"]["text"], record["response_text"], 0
        )
        if error:
            raise RuntimeError(f"{record['arm']}: {error}")
        key = (record["arm"], "WFM01")
        previous = next(s for s in scores if (s["arm"], s["question_id"]) == key)
        replacements[key] = {
            **previous,
            **values,
            "quality_mean": sum(values[d] for d in judge.DIMENSIONS) / len(judge.DIMENSIONS),
            "evaluation_revision": "fulltext-v2",
            "evaluated_response_chars": len(record["response_text"]),
            "response_sha256": hashlib.sha256(record["response_text"].encode()).hexdigest(),
            "error": None,
        }
        print(record["arm"], replacements[key]["quality_mean"], flush=True)
    with destination.open("x", encoding="utf-8") as stream:
        for score in scores:
            row = replacements.get((score["arm"], score["question_id"]), score)
            stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"Verified 11 full-text replacements, 187 total scores: {destination}", flush=True)


if __name__ == "__main__":
    main()
