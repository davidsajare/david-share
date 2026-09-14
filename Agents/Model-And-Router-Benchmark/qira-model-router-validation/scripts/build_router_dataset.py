"""
Assemble datasets/router_taskb.jsonl for Task B.

Two sources, one file, so every router arm and every direct baseline sees the
identical prompt set:

  1. datasets/router_questions.jsonl - 30 questions in three complexity tiers
     (S/M/C x 10), each tagged with a category. This is the controlled probe of
     "which cases switch".
  2. datasets/qira_scenarios.jsonl - the 17 Qira workshop prompts used in Task A.
     They carry no tier, so a tier is assigned here from the task's cognitive
     demand (not from prompt length), and recorded as `tier_source=assigned`
     so the report can separate controlled tiers from assigned ones.

Run: python scripts/build_router_dataset.py
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC_TIERED = ROOT / "datasets" / "router_questions.jsonl"
SRC_QIRA = ROOT / "datasets" / "qira_scenarios.jsonl"
OUT = ROOT / "datasets" / "router_taskb.jsonl"

# Tier assignment for Qira prompts. Rationale per item, because this is the
# ground truth the routing hit-rate is judged against.
QIRA_TIERS = {
    # simple: one-shot, short answer, little inference
    "NM03": "simple",    # one-sentence next action
    "CMU03": "simple",   # condense to one sentence
    "PA03": "simple",    # instant recall from a transcript, one sentence
    "LI02": "simple",    # "fix it for me" - produce a corrected formula
    "CZ02": "simple",    # parse an edit intent into a structured list
    # moderate: transformation with constraints
    "NM02": "moderate",  # cross-device continuity suggestion
    "WFM02": "moderate", # continue in the same voice
    "WFM03": "moderate", # rewrite for an executive, preserve every fact
    "WFM04": "moderate", # short escalation email
    "CMU02": "moderate", # decision extraction with owners and deadlines
    "PA02": "moderate",  # translate + summarize in Chinese
    "LI01": "moderate",  # diagnose a spreadsheet formula error conversationally
    "CZ01": "moderate",  # expand a rough image idea into a detailed prompt
    # complex: multi-source synthesis, prioritization, long-form structure
    "NM01": "complex",   # prioritize three actions across three signals with rationale
    "WFM01": "complex",  # full project proposal with several required sections
    "CMU01": "complex",  # three-day backlog into a prioritized digest
    "PA01": "complex",   # key points + decisions + owners from a long transcript
}


def main() -> None:
    items: list[dict] = []
    seen: set[str] = set()

    for line in SRC_TIERED.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        item.setdefault("source", "router_questions")
        item["tier_source"] = "controlled"
        items.append(item)
        seen.add(item["id"])

    for line in SRC_QIRA.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        item = json.loads(line)
        if item["id"] in seen:
            raise SystemExit(f"duplicate id across sources: {item['id']}")
        tier = QIRA_TIERS.get(item["id"])
        if tier is None:
            raise SystemExit(f"no tier assigned for Qira item {item['id']}")
        item["tier"] = tier
        item["tier_source"] = "assigned"
        item["source"] = "qira_scenarios"
        item.setdefault("category", item.get("task_type"))
        items.append(item)
        seen.add(item["id"])

    with OUT.open("w", encoding="utf-8") as fh:
        for item in items:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    tiers = {}
    for item in items:
        tiers[item["tier"]] = tiers.get(item["tier"], 0) + 1
    print(f"wrote {OUT.relative_to(ROOT)}: {len(items)} items, tiers={tiers}")


if __name__ == "__main__":
    main()
