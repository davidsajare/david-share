"""Build or verify the immutable manifest for the Qira repository split.

The scientific project used to live under AOAI-Model-Migration-Benchmark.
This tool compares every source blob at the pinned pre-split commit with its
new location. Only the small, declared set of path-dependent files may differ.

Usage:
    python scripts/build_split_manifest.py
    python scripts/build_split_manifest.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
OUT = ROOT / "evidence" / "split-manifest.json"

BASELINE = "af65768bf2ddc88f7c45432598848cd233fc4aa3"
OLD_ROOT = "Agents/AOAI-Model-Migration-Benchmark"
NEW_ROOT = "Agents/Model-And-Router-Benchmark"
FOLDERS = (
    "qira-scenario-model-benchmark",
    "qira-model-router-validation",
    "qira-followup-throughput-recalibration",
    "qira-production-readiness",
    "qira-live-benchmark-console",
)

CONTENT_CHANGES = {
    "qira-live-benchmark-console/README.md":
        "Reproduction command points to the independent repository.",
    "qira-live-benchmark-console/README-CN.md":
        "Chinese reproduction command points to the independent repository.",
    "qira-live-benchmark-console/deploy/demo-portal-card.html":
        "The Demo Portal Source link points to the independent repository.",
    "qira-live-benchmark-console/static/styles.css":
        "The L5 UI gate removed desktop intrinsic-width overflow on mobile.",
    "qira-live-benchmark-console/tests/test_console.py":
        "A regression test pins the mobile layout contract.",
    "qira-model-router-validation/README.md":
        "Generated reproduction command points to the independent repository.",
    "qira-model-router-validation/README-CN.md":
        "Generated Chinese reproduction command points to the independent repository.",
    "qira-model-router-validation/scripts/build_router_readme.py":
        "The source-of-truth README builder emits the independent path.",
    "qira-model-router-validation/scripts/validate_router_readme.py":
        "The link boundary is the new independent project root, with a traversal test.",
    "qira-scenario-model-benchmark/README.md":
        "The parent overview link now describes this independent project.",
    "qira-scenario-model-benchmark/README-CN.md":
        "The Chinese parent overview link now describes this independent project.",
}
SCIENTIFIC_PARTS = {"config", "datasets", "outputs", "replay"}


class ManifestError(RuntimeError):
    """The split no longer matches its immutable source."""


def git(*args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", *args],
        cwd=REPO,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if result.returncode:
        raise ManifestError(
            f"git {' '.join(args)} failed: {result.stderr.strip()}"
        )
    return result.stdout


def source_blobs() -> dict[str, str]:
    helper = f"{OLD_ROOT}/scripts/withhold_qira_transcript_prompts.py"
    prefixes = [f"{OLD_ROOT}/{folder}" for folder in FOLDERS]
    output = git("ls-tree", "-r", BASELINE, "--", *prefixes, helper)
    entries: dict[str, str] = {}
    for line in output.splitlines():
        metadata, path = line.split("\t", 1)
        _, object_type, object_id = metadata.split()
        if object_type != "blob":
            continue
        entries[path] = object_id
    return entries


def index_blobs() -> dict[str, str]:
    helper = f"{NEW_ROOT}/scripts/withhold_qira_transcript_prompts.py"
    output = git("ls-files", "--stage", "--", f"{NEW_ROOT}/qira-*", helper)
    entries: dict[str, str] = {}
    for line in output.splitlines():
        metadata, path = line.split("\t", 1)
        _, object_id, stage = metadata.split()
        if stage != "0":
            raise ManifestError(f"unmerged index entry: {path}")
        entries[path] = object_id
    return entries


def require_no_unstaged_destination_changes() -> None:
    helper = f"{NEW_ROOT}/scripts/withhold_qira_transcript_prompts.py"
    result = subprocess.run(
        [
            "git", "diff", "--quiet", "--",
            f"{NEW_ROOT}/qira-*", helper,
        ],
        cwd=REPO,
        check=False,
    )
    if result.returncode == 1:
        raise ManifestError(
            "destination has unstaged changes; stage it before building the manifest"
        )
    if result.returncode:
        raise ManifestError(f"git diff failed with exit code {result.returncode}")


def logical_path(source: str) -> str:
    prefix = f"{OLD_ROOT}/"
    if not source.startswith(prefix):
        raise ManifestError(f"source path left the expected root: {source}")
    return source[len(prefix):]


def digest(entries: list[tuple[str, str]]) -> str:
    payload = "".join(f"{path}\0{blob}\n" for path, blob in sorted(entries))
    return hashlib.sha256(payload.encode()).hexdigest()


def build() -> dict:
    resolved = str(git("rev-parse", BASELINE)).strip()
    if resolved != BASELINE:
        raise ManifestError(f"baseline resolved to {resolved}, expected {BASELINE}")

    source_index = source_blobs()
    helper_source = f"{OLD_ROOT}/scripts/withhold_qira_transcript_prompts.py"
    helper_destination = f"{NEW_ROOT}/scripts/withhold_qira_transcript_prompts.py"
    sources = sorted(
        path for path in source_index
        if any(path.startswith(f"{OLD_ROOT}/{folder}/") for folder in FOLDERS)
    )
    if not sources:
        raise ManifestError("the immutable source tree is empty")

    require_no_unstaged_destination_changes()
    current_index = index_blobs()
    expected_destinations = {
        f"{NEW_ROOT}/{logical_path(source)}" for source in sources
    }
    current = {path for path in current_index if path != helper_destination}
    missing = sorted(expected_destinations - current)
    unexpected = sorted(current - expected_destinations)
    if missing or unexpected:
        raise ManifestError(
            f"move inventory drift; missing={missing[:5]}, unexpected={unexpected[:5]}"
        )

    unchanged: list[tuple[str, str]] = []
    scientific: list[tuple[str, str]] = []
    changes: list[dict] = []
    mismatches: list[str] = []

    for source in sources:
        logical = logical_path(source)
        destination = f"{NEW_ROOT}/{logical}"
        before = source_index[source]
        after = current_index[destination]
        if logical in CONTENT_CHANGES:
            changes.append({
                "path": logical,
                "reason": CONTENT_CHANGES[logical],
                "source_blob": before,
                "destination_blob": after,
            })
            continue
        if before != after:
            mismatches.append(logical)
            continue
        unchanged.append((logical, before))
        if SCIENTIFIC_PARTS.intersection(Path(logical).parts):
            scientific.append((logical, before))

    if mismatches:
        raise ManifestError(
            "undeclared content changed during the split: " + ", ".join(mismatches[:12])
        )

    declared = set(CONTENT_CHANGES)
    observed = {entry["path"] for entry in changes}
    if declared != observed:
        raise ManifestError(
            f"declared content-change set drift; missing={sorted(declared - observed)}"
        )

    return {
        "schema_version": 1,
        "source": {
            "commit": BASELINE,
            "root": OLD_ROOT,
            "folders": list(FOLDERS),
            "file_count": len(sources),
        },
        "destination": {
            "root": NEW_ROOT,
            "file_count": len(current),
        },
        "byte_identity": {
            "verified": True,
            "unchanged_file_count": len(unchanged),
            "logical_tree_sha256": digest(unchanged),
        },
        "scientific_artifacts": {
            "verified_byte_identical": True,
            "parts": sorted(SCIENTIFIC_PARTS),
            "file_count": len(scientific),
            "logical_tree_sha256": digest(scientific),
        },
        "intentional_content_changes": sorted(changes, key=lambda item: item["path"]),
        "moved_helper": {
            "source": helper_source,
            "destination": helper_destination,
            "source_blob": source_index[helper_source],
            "destination_blob": current_index[helper_destination],
            "reason": "The usage path changed with the owning repository.",
        },
    }


def render(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail if the committed manifest differs; write nothing",
    )
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
            print(
                "STALE: split-manifest.json differs from the immutable source "
                "or current destination"
            )
            return 1
        data = json.loads(rendered)
        print(
            "VERIFIED: "
            f"{data['source']['file_count']} moved files; "
            f"{data['scientific_artifacts']['file_count']} scientific artifacts "
            "byte-identical"
        )
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(rendered, encoding="utf-8", newline="\n")
    data = json.loads(rendered)
    print(
        f"Wrote {OUT.relative_to(ROOT)}: "
        f"{data['byte_identity']['unchanged_file_count']} unchanged files, "
        f"{len(data['intentional_content_changes'])} declared path edits"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
