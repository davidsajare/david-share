"""Cross-check the retained full-text answers against the numerical evidence.

Proves, offline and without calling any model, that every ``response_text`` in
``outputs/raw_fulltext/direct_<run>.jsonl`` hashes to the ``response_sha256``
stored in the numerical record with the same (arm, question_id, iteration) key,
and that the numerical fields of both files agree.

Usage:
    python scripts/verify_fulltext.py

Author: Xinyu Wei (魏新宇)
"""

import hashlib
import json
import lzma
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = "20260909_120534"
OUTPUT = ROOT / "outputs"
KEY = ("arm", "question_id", "iteration")
IGNORED = {"response_text", "response_preview", "response_sha256", "cost_usd",
           "total_tokens", "visible_output_tokens_estimate"}
WITHHELD_IDS = {"PA01", "PA03"}
MARKER = ("[withheld in the public copy: this cell reproduced an internal meeting "
          "transcript; see outputs/public_redaction.json]")
PUBLIC_ARCHIVE_SHA256 = "e3ed47adfa25c0da1234496095436a880fa0d16a168ed6aecf0efa0eb64a94a8"
PUBLIC_NUMERIC_SHA256 = "afa504454ed278b5f3fbcfe853d1e6c1320f682777c9d10d89dfbd2de276722e"
PUBLIC_FULLTEXT_SHA256 = "aae524f8263fe77bd07100fa41e0b97379272499f8e7cf8e5c62ce160ddfff8e"


def load(path: Path) -> dict:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    keyed = {tuple(r[k] for k in KEY): r for r in rows}
    if len(keyed) != len(rows):
        raise ValueError(f"{path.name}: duplicate matrix cells")
    return keyed


def main() -> None:
    archive_path = OUTPUT / f"evidence_{RUN}.json.xz"
    numeric_path = OUTPUT / f"direct_{RUN}.metrics.jsonl"
    fulltext_path = OUTPUT / f"raw_fulltext/direct_{RUN}.jsonl"
    for path, expected in (
        (archive_path, PUBLIC_ARCHIVE_SHA256),
        (numeric_path, PUBLIC_NUMERIC_SHA256),
        (fulltext_path, PUBLIC_FULLTEXT_SHA256),
    ):
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"{path.name}: SHA256 {actual} differs from pinned public evidence.")

    evidence = json.loads(lzma.decompress(archive_path.read_bytes()))
    archived = {
        tuple(row[k] for k in KEY): row
        for row in (
            dict(zip(evidence["performance_columns"], values, strict=True))
            for values in evidence["performance_values"]
        )
    }
    numeric = load(numeric_path)
    fulltext = load(fulltext_path)
    if set(numeric) != set(fulltext) or len(numeric) != 748:
        raise ValueError("Full-text and numerical files do not cover the same 748 cells.")
    if set(numeric) != set(archived):
        raise ValueError("Numerical evidence and the pinned archive cover different cells.")
    mismatched_hash, mismatched_fields, withheld = [], [], 0
    for key, record in numeric.items():
        answer = fulltext[key]
        if record["response_sha256"] != archived[key]["response_sha256"]:
            mismatched_hash.append(key)
        if key[1] in WITHHELD_IDS:
            if answer["response_text"] != MARKER:
                raise ValueError(f"withheld cell {key} does not carry the public-redaction marker")
            withheld += 1
        elif hashlib.sha256(answer["response_text"].encode()).hexdigest() != record["response_sha256"]:
            mismatched_hash.append(key)
        for field, value in record.items():
            if field not in IGNORED and answer.get(field) != value:
                mismatched_fields.append((key, field))
    if mismatched_hash or mismatched_fields:
        raise ValueError(f"hash mismatches={mismatched_hash[:5]} field mismatches={mismatched_fields[:5]}")
    chars = sum(len(a["response_text"]) for k, a in fulltext.items() if k[1] not in WITHHELD_IDS)
    note = f"; {withheld} answers to {sorted(WITHHELD_IDS)} withheld in the public copy (hashes anchored in the archive)" if withheld else ""
    print(f"VERIFIED: {748 - withheld} answers match their SHA256 and numerical fields; {chars:,} characters of model output retained{note}.")


if __name__ == "__main__":
    main()
