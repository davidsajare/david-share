"""Cross-check the retained Task B full-text answers against the numerical evidence.

Proves, offline and without calling any model, that every ``response_text`` in
``outputs/raw_fulltext/router_<run>.jsonl`` hashes to the ``response_sha256``
stored in ``outputs/router_<run>.metrics.jsonl`` for the same
(arm, question_id, iteration) cell, and that the numerical fields agree.

Usage:
    python scripts/verify_router_fulltext.py

Author: Xinyu Wei (魏新宇)
"""

import hashlib
import json
import lzma
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = "20260909_223737"
EXPECTED = 1880
OUTPUT = ROOT / "outputs"
KEY = ("arm", "question_id", "iteration")
# Fields the builder adds or derives; they do not exist in the raw harness output.
IGNORED = {"response_text", "response_preview", "response_sha256", "cost_usd",
           "total_tokens", "visible_output_tokens_estimate", "category", "source"}
WITHHELD_IDS = {"PA01", "PA03"}
MARKER = ("[withheld in the public copy: this cell reproduced an internal meeting "
          "transcript; see outputs/public_redaction.json]")
PUBLIC_ARCHIVE_SHA256 = "37e5dfa30117d687f175ed4bcae748bb4bba432484bdd493324cba7cd45c4061"
PUBLIC_NUMERIC_SHA256 = "2c7996682fd5091c8509c1eb107cc57b6cbfa6b6b6af33c814516c7e9713b589"
PUBLIC_FULLTEXT_SHA256 = "0d0b56d0f4678eaff18e09b9f4697edfac7fb0194b43c378f113aee293d6d1b6"
PUBLIC_QUALITY_SHA256 = "c1ef3d4909816c8a335280a7cc5173aef13aaf3c523fc91ea3554c87abbdb453"


def load(path: Path) -> dict:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    keyed = {tuple(r[k] for k in KEY): r for r in rows}
    if len(keyed) != len(rows):
        raise ValueError(f"{path.name}: duplicate matrix cells")
    return keyed


def main() -> None:
    archive_path = OUTPUT / f"evidence_router_{RUN}.json.xz"
    numeric_path = OUTPUT / f"router_{RUN}.metrics.jsonl"
    raw_path = OUTPUT / f"raw_fulltext/router_{RUN}.jsonl"
    quality_path = OUTPUT / f"raw_fulltext/quality_{RUN}.jsonl"
    generated_quality_path = OUTPUT / f"quality_router_{RUN}.jsonl"
    for path, expected in (
        (archive_path, PUBLIC_ARCHIVE_SHA256),
        (numeric_path, PUBLIC_NUMERIC_SHA256),
        (raw_path, PUBLIC_FULLTEXT_SHA256),
        (quality_path, PUBLIC_QUALITY_SHA256),
        (generated_quality_path, PUBLIC_QUALITY_SHA256),
    ):
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise ValueError(f"{path.name}: SHA256 {actual} differs from pinned public evidence.")
    if load(quality_path) != load(OUTPUT / f"quality_router_{RUN}.jsonl"):
        raise ValueError("Retained judge scores differ from the generated quality file.")
    evidence = json.loads(lzma.decompress(archive_path.read_bytes()))
    archived = {
        tuple(row[k] for k in KEY): row
        for row in (
            dict(zip(evidence["performance_columns"], values, strict=True))
            for values in evidence["performance_values"]
        )
    }
    numeric = load(numeric_path)
    fulltext = load(raw_path)
    if set(numeric) != set(fulltext) or len(numeric) != EXPECTED:
        raise ValueError(f"Full-text and numerical files do not cover the same {EXPECTED} cells.")
    if set(numeric) != set(archived):
        raise ValueError("Numerical evidence and the pinned archive cover different cells.")
    mismatched_hash, mismatched_fields, withheld = [], [], 0
    for key, record in numeric.items():
        answer = fulltext[key]
        if record["response_sha256"] != archived[key]["response_sha256"]:
            mismatched_hash.append(key)
        if key[1] in WITHHELD_IDS:
            if answer.get("response_text") != MARKER:
                raise ValueError(f"withheld cell {key} does not carry the public-redaction marker")
            withheld += 1
        elif hashlib.sha256((answer.get("response_text") or "").encode()).hexdigest() != record["response_sha256"]:
            mismatched_hash.append(key)
        for field, value in record.items():
            if field not in IGNORED and answer.get(field) != value:
                mismatched_fields.append((key, field))
    if mismatched_hash or mismatched_fields:
        raise ValueError(f"hash mismatches={mismatched_hash[:5]} field mismatches={mismatched_fields[:5]}")
    chars = sum(len(a["response_text"]) for k, a in fulltext.items() if k[1] not in WITHHELD_IDS)
    served = sum(1 for a in fulltext.values() if a.get("served_model_family") == "gpt-5.6-sol")
    note = f"; {withheld} answers to {sorted(WITHHELD_IDS)} withheld in the public copy (hashes anchored in the archive)" if withheld else ""
    print(f"VERIFIED: {EXPECTED - withheld} answers match their SHA256 and numerical fields; "
          f"{chars:,} characters of model output retained; {served} answers were served by gpt-5.6-sol{note}.")


if __name__ == "__main__":
    main()
