"""Build or verify the de-identification manifest for this benchmark project.

The public copy of this project was de-identified: a customer product name, a
venue, a demo endpoint and a shared login were removed; folders and two datasets
were renamed; and one dependent session whose answers echoed a customer
identifier was withheld.

The question a reader should ask is whether the numbers moved while that
happened. This tool answers it mechanically. `evidence/baseline-blobs.json`
pins the pre-de-identification content of every retained file by its *current*
path, so this verifier never has to repeat what was removed. For each changed
evidence file it walks both copies structurally and compares every numeric leaf.
Any drift fails.

Usage:
    python scripts/build_deid_manifest.py
    python scripts/build_deid_manifest.py --check
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
BASELINE_BLOBS = ROOT / "evidence" / "baseline-blobs.json"
OUT = ROOT / "evidence" / "deidentification-manifest.json"

# Evidence trees whose numbers must survive de-identification untouched.
NUMERIC_PARTS = ("outputs", "datasets", "config", "replay")
NUMERIC_SUFFIXES = {".json", ".jsonl", ".csv"}

# Bookkeeping files that index other files by path. Renaming a dataset moves
# their keys, so a structural compare would flag the rename itself rather than
# a measurement. They hold no measured value.
LEDGER_NAMES = {"public_redaction.json", "manifest.json"}
LEDGER_PREFIXES = ("provenance", "resource_closeout", "deployment_verification")

REMOVED_SUMMARY = {
    "console screenshots of a private demo deployment": 2,
    "a UI-evidence file recording that deployment's endpoint": 1,
    "a portal card carrying a demo URL and a shared login": 1,
    "the superseded split manifest and its builder": 2,
}

LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/"


class ManifestError(RuntimeError):
    """The public copy no longer agrees with its pinned baseline."""


def git_bytes(*args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=REPO, check=False, capture_output=True)
    if result.returncode:
        raise ManifestError(f"git {' '.join(args)} failed")
    return result.stdout


def resolve_lfs(raw: bytes) -> bytes:
    """Expand a Git LFS pointer to the content the working tree would hold."""
    if not raw.startswith(LFS_POINTER_PREFIX):
        return raw
    result = subprocess.run(["git", "lfs", "smudge"], cwd=REPO,
                            input=raw, capture_output=True, check=False)
    if result.returncode or result.stdout.startswith(LFS_POINTER_PREFIX):
        raise ManifestError(
            "cannot expand a Git LFS pointer; run 'git lfs pull' for this project")
    return result.stdout


def current_index(project: str) -> dict[str, str]:
    """Blob id of every tracked file under the project, keyed by relative path."""
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "--stage", "--", project],
        cwd=REPO, check=False, capture_output=True, text=True, encoding="utf-8",
    )
    if result.returncode:
        raise ManifestError("git ls-files --stage failed")
    entries: dict[str, str] = {}
    for line in result.stdout.splitlines():
        if not line:
            continue
        metadata, path = line.split("\t", 1)
        _, oid, stage = metadata.split()
        if stage != "0":
            raise ManifestError(f"unmerged index entry: {path}")
        entries[path[len(project) + 1:]] = oid
    return entries


def numeric_leaves(value, path: str = "") -> dict[str, float]:
    """Every numeric leaf in a parsed document, keyed by its structural path."""
    found: dict[str, float] = {}
    if isinstance(value, bool):
        return found
    if isinstance(value, (int, float)):
        found[path] = float(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            found.update(numeric_leaves(item, f"{path}/{key}"))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.update(numeric_leaves(item, f"{path}[{index}]"))
    return found


def parse_numbers(name: str, raw: bytes) -> dict[str, float]:
    text = raw.decode("utf-8-sig")
    if name.endswith(".jsonl"):
        numbers: dict[str, float] = {}
        for index, line in enumerate(text.splitlines()):
            if line.strip():
                numbers.update(numeric_leaves(json.loads(line), f"[{index}]"))
        return numbers
    if name.endswith(".json"):
        return numeric_leaves(json.loads(text))
    numbers = {}
    for row_index, row in enumerate(csv.DictReader(io.StringIO(text))):
        for column, cell in row.items():
            if cell in (None, ""):
                continue
            try:
                numbers[f"[{row_index}]/{column}"] = float(cell)
            except (TypeError, ValueError):
                continue
    return numbers


def is_numeric_evidence(relative: str) -> bool:
    path = Path(relative)
    if path.name in LEDGER_NAMES or path.name.startswith(LEDGER_PREFIXES):
        return False
    return path.suffix in NUMERIC_SUFFIXES and any(
        part in NUMERIC_PARTS for part in path.parts)


def build() -> dict:
    pinned = json.loads(BASELINE_BLOBS.read_text(encoding="utf-8"))
    baseline_commit = pinned["baseline_commit"]
    blobs: dict[str, str] = pinned["blobs"]

    compared = 0
    values_compared = 0
    unchanged = 0
    changed = 0
    missing: list[str] = []
    drift: list[str] = []

    project = f"Agents/{ROOT.name}"
    indexed = current_index(project)

    for relative, oid in sorted(blobs.items()):
        current_oid = indexed.get(relative)
        if current_oid is None:
            missing.append(relative)
            continue
        before = resolve_lfs(git_bytes("cat-file", "blob", oid))
        # Compare the committed blob, not the working tree, so a checkout that
        # normalizes line endings cannot change the verdict by platform.
        after = resolve_lfs(git_bytes("cat-file", "blob", current_oid))
        if before == after:
            unchanged += 1
            continue
        changed += 1
        if not is_numeric_evidence(relative):
            continue
        compared += 1
        try:
            old_numbers = parse_numbers(relative, before)
            new_numbers = parse_numbers(relative, after)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ManifestError(f"{relative}: cannot re-read for numeric compare: {exc}") from exc
        values_compared += len(old_numbers)
        if old_numbers != new_numbers:
            differing = sorted(
                key for key in set(old_numbers) | set(new_numbers)
                if old_numbers.get(key) != new_numbers.get(key))
            drift.append(f"{relative}: {len(differing)} numeric field(s), first {differing[:3]}")

    if missing:
        raise ManifestError("pinned files missing from the tree: " + ", ".join(missing[:8]))
    if drift:
        raise ManifestError("de-identification changed measurements:\n" + "\n".join(drift[:8]))

    return {
        "schema_version": 2,
        "purpose": (
            "Prove that removing a customer identity, a demo endpoint and a shared "
            "login from the public copy did not alter any measurement."
        ),
        "baseline_commit": baseline_commit,
        "removed_from_public_copy": dict(sorted(REMOVED_SUMMARY.items())),
        "files": {
            "pinned": len(blobs),
            "byte_identical": unchanged,
            "de_identified": changed,
        },
        "numeric_identity": {
            "verified": True,
            "scope": (
                "Every numeric leaf of every changed measurement, dataset, config "
                "and replay file. Path-indexed ledgers are excluded because a "
                "rename moves their keys and they hold no measured value."
            ),
            "excluded_ledgers": sorted(LEDGER_NAMES) + [f"{p}*" for p in LEDGER_PREFIXES],
            "evidence_files_compared": compared,
            "numeric_values_compared": values_compared,
            "mismatches": 0,
        },
    }


def render(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="fail if the committed manifest differs; write nothing")
    args = parser.parse_args()
    try:
        rendered = render(build())
    except ManifestError as exc:
        print(f"FAILED: {exc}")
        return 1

    if args.check:
        if not OUT.is_file():
            print(f"MISSING: {OUT.relative_to(ROOT)}")
            return 1
        if OUT.read_text(encoding="utf-8") != rendered:
            print("STALE: deidentification-manifest.json no longer matches the tree")
            return 1
        data = json.loads(rendered)
        identity = data["numeric_identity"]
        print(
            f"VERIFIED: {identity['numeric_values_compared']} numeric values across "
            f"{identity['evidence_files_compared']} de-identified evidence files, "
            "0 mismatches"
        )
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(rendered, encoding="utf-8", newline="\n")
    data = json.loads(rendered)
    print(
        f"Wrote {OUT.relative_to(ROOT)}: {data['files']['byte_identical']} byte-identical, "
        f"{data['files']['de_identified']} de-identified, "
        f"{data['numeric_identity']['numeric_values_compared']} numeric values verified"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
