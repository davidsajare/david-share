"""Withhold the two Qira prompts that reproduce an internal meeting transcript, for the public copy.

PA01 and PA03 in datasets/qira_scenarios.jsonl (and their copies in router_taskb.jsonl) were
written as a synthetic meeting transcript that names colleagues and mirrors an internal
planning discussion. In the public repository their prompt text, every model answer to them,
and every judge justification about them are replaced by MARKER. Nothing numerical changes:
token counts, latency, cost, scores and the per-answer response_sha256 stay exactly as measured,
so the withheld cells remain in every aggregate and every matrix check still passes.

The evidence builders and full-text verifiers pin the public archive and file
digests independently. For withheld cells they assert MARKER and compare the
retained response_sha256 with the same cell in the pinned archive.

Idempotent: rows already carrying MARKER are left alone and re-recorded. Source
metadata was sanitized before publication; its original/public digests remain
in the existing manifests, but this public script does not contain the source
strings that were removed.

Usage (from Agents/Model-And-Router-Benchmark):
    python scripts/withhold_qira_transcript_prompts.py

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import hashlib
import json
import lzma
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FOLDERS = ("qira-scenario-model-benchmark", "qira-model-router-validation",
           "qira-followup-throughput-recalibration", "qira-production-readiness")
WITHHELD_IDS = {"PA01", "PA03"}
MARKER = "[withheld in the public copy: this cell reproduced an internal meeting transcript; see outputs/public_redaction.json]"
TEXT_FIELDS = ("response_text", "response_preview", "justification")
REASON = ("Prompts PA01 and PA03 were a synthetic meeting transcript naming colleagues and mirroring an internal "
          "planning discussion. Their prompt text, the model answers to them and the judge justifications about them "
          "are withheld from the public copy. All numerical fields, scores and response_sha256 values are unchanged, "
          "so every aggregate and matrix check still covers these cells.")


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def qid(row: dict):
    return row.get("question_id") or row.get("id")


def redact_rows(rows: list[dict]) -> int:
    changed = 0
    for row in rows:
        if qid(row) not in WITHHELD_IDS:
            continue
        for field in TEXT_FIELDS:
            if field in row and row[field] != MARKER:
                row[field] = MARKER
                changed += 1
        if "text" in row and "turns" not in row and row["text"] != MARKER:
            row["text"] = MARKER
            row["public_withheld"] = True
            changed += 1
    return changed


def process_jsonl(path: Path, base: Path, record: dict) -> None:
    original = path.read_bytes()
    rows = [json.loads(line) for line in original.decode("utf-8").splitlines() if line.strip()]
    changed = redact_rows(rows)
    rel = path.relative_to(base).as_posix()
    if changed == 0 and rel not in record:
        return  # no text fields to withhold (numeric-only file) or nothing new
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    path.write_text(text, encoding="utf-8", newline="\n")
    record[rel] = {"original_sha256": record.get(rel, {}).get("original_sha256") or sha(original),
                   "redacted_sha256": sha(path.read_bytes()),
                   "rows_withheld": sum(1 for r in rows if qid(r) in WITHHELD_IDS)}


def process_archive(path: Path, base: Path, record: dict) -> None:
    original = path.read_bytes()
    evidence = json.loads(lzma.decompress(original))
    changed = redact_rows(evidence.get("quality", []))
    rel = path.relative_to(base).as_posix()
    if changed == 0 and rel not in record:
        return
    if changed:
        path.write_bytes(lzma.compress(json.dumps(evidence, separators=(",", ":")).encode()))
    record[rel] = {"original_sha256": record.get(rel, {}).get("original_sha256") or sha(original),
                   "redacted_sha256": sha(path.read_bytes()),
                   "rows_withheld": sum(1 for r in evidence.get("quality", []) if qid(r) in WITHHELD_IDS)}


def main() -> None:
    for folder in FOLDERS:
        base = ROOT / folder
        if not base.exists():
            raise SystemExit(f"missing folder {folder}")
        redaction_path = base / "outputs" / "public_redaction.json"
        previous = json.loads(redaction_path.read_text(encoding="utf-8")) if redaction_path.exists() else {}
        record: dict = dict(previous.get("files", {}))
        for path in sorted(list((base / "datasets").glob("*.jsonl")) + list((base / "outputs").rglob("*.jsonl"))):
            process_jsonl(path, base, record)
        for path in sorted((base / "outputs").rglob("*.xz")):
            process_archive(path, base, record)
        redaction_path.write_text(json.dumps({
            "marker": MARKER, "withheld_question_ids": sorted(WITHHELD_IDS), "reason": REASON,
            "files": dict(sorted(record.items())),
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        print(f"{folder}: {len(record)} public transformations recorded in "
              f"{redaction_path.relative_to(ROOT).as_posix()}")


if __name__ == "__main__":
    main()
