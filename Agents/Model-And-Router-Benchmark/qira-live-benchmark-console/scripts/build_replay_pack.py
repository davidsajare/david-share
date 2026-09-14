"""
Build the console's replay pack from the recorded study runs.

The pack lets the console demonstrate every chart with no credentials, no
network and no spend - useful for a rehearsal, a laptop on a plane, or the
moment the conference-room wifi dies five minutes before the session.

Replay summaries are produced by the same bench_core.summarize_arm() the live
path uses, from the same raw .metrics.jsonl the written reports were built
from. Nothing is recomputed by hand, so a replayed chart and a live chart mean
the same thing.

Usage:
    python scripts/build_replay_pack.py [--check]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import bench_core  # noqa: E402  - needs the path insert above

PARENT = ROOT.parent
OUT = ROOT / "replay" / "replay_pack.json"

# Each entry: folder, glob for the raw metrics, title and the one-line reason a
# customer would care about this run.
SOURCES = [
    {
        "id": "scenario-matrix",
        "folder": "qira-scenario-model-benchmark",
        "glob": "outputs/*.metrics.jsonl",
        "title": "Qira scenarios across the candidate models",
        "description": "Single-turn, concurrency 1, Sweden Central VM to Sweden Central deployments, no tools.",
    },
    {
        "id": "router-validation",
        "folder": "qira-model-router-validation",
        "glob": "outputs/*.metrics.jsonl",
        "title": "Model Router against its own direct baselines",
        "description": "Three router modes versus direct Sol and Luna on identical prompts, Chat Completions for every arm.",
    },
    {
        "id": "throughput-recalibration",
        "folder": "qira-followup-throughput-recalibration",
        "glob": "outputs/raw_fulltext/direct_*.jsonl",
        "title": "Throughput re-measurement",
        "description": "The follow-up run that recalibrated decode throughput after the first pass.",
    },
    {
        "id": "production-readiness",
        "folder": "qira-production-readiness",
        "glob": "outputs/raw_fulltext/sustained_*.numeric.jsonl",
        "title": "Sustained concurrency",
        "description": "Steady-state behaviour under sustained load rather than one request at a time.",
    },
]


def load_records(path: Path) -> list[dict]:
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def build_run(source: dict) -> dict | None:
    folder = PARENT / source["folder"]
    if not folder.is_dir():
        return None
    files = sorted(folder.glob(source["glob"]))
    if not files:
        return None

    records: list[dict] = []
    for path in files:
        records.extend(load_records(path))

    measured = [r for r in records if not r.get("warmup")]
    if not measured:
        return None

    by_arm: dict[str, list[dict]] = {}
    for record in measured:
        by_arm.setdefault(record.get("arm") or record.get("deployment") or "unknown", []).append(record)

    summaries = [bench_core.summarize_arm(arm, rows) for arm, rows in sorted(by_arm.items())]
    priced = [s["cost_per_1k_requests"] for s in summaries if s.get("cost_per_1k_requests")]
    baseline = max(priced) if priced else None
    for summary in summaries:
        summary["value_ratio"] = bench_core.value_score(summary, baseline)

    return {
        "id": source["id"],
        "title": source["title"],
        "description": source["description"],
        "folder": source["folder"],
        "files": [f.name for f in files],
        "records": len(measured),
        "summaries": summaries,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="Verify the committed pack matches the recorded runs; write nothing.")
    args = parser.parse_args()

    runs = [run for run in (build_run(s) for s in SOURCES) if run]
    if not runs:
        print("No recorded study runs found next to the console; nothing to build.")
        return 1

    pack = {
        "_comment": "Recorded runs re-aggregated by bench_core.summarize_arm. "
                    "Replayed measurements, not a live test.",
        # Kept in the non-LFS pack so a clone without git-lfs can still open
        # replay mode. Live mode continues to read the sibling study assets.
        "catalog": bench_core.catalog(),
        "runs": runs,
    }
    rendered = json.dumps(pack, ensure_ascii=False, indent=2) + "\n"

    if args.check:
        if not OUT.is_file():
            print(f"MISSING: {OUT.relative_to(ROOT)} has not been built.")
            return 1
        if OUT.read_text(encoding="utf-8") != rendered:
            print(f"STALE: {OUT.relative_to(ROOT)} differs from the recorded runs. Re-run without --check.")
            return 1
        print(f"VERIFIED: {len(runs)} recorded run(s), "
              f"{sum(len(r['summaries']) for r in runs)} arm summaries.")
        return 0

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(rendered, encoding="utf-8", newline="\n")
    for run in runs:
        print(f"  {run['id']:<26} {run['records']:>6} records  {len(run['summaries']):>2} arms")
    print(f"Wrote {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
