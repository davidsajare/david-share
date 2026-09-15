"""Prepare a compact, checksummed numerical export of a Task B (router) run.

Runs ON the benchmark VM. Strips the model answers (keeping their SHA256) so
the archive is small enough to leave the VM through Run Command, and records
the VM region from Azure IMDS so the report can prove where the load came from.

Usage (on the VM):
    .venv/bin/python scripts/export_router_evidence.py                 # final: 1880 rows + 470 scores
    .venv/bin/python scripts/export_router_evidence.py --allow-partial # while the harness is still running

Author: Xinyu Wei (魏新宇)
"""

import argparse
import hashlib
import importlib.metadata
import json
import lzma
import platform
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
RUN = "20260909_223737"
EXPECTED_ROWS = 1880      # 10 arms x 47 questions x (1 warm-up + 3 measured)
EXPECTED_SCORES = 470     # 10 arms x 47 questions, one judged answer per cell
OMITTED = ("response_text", "response_preview")
SOURCES = ("harness.py", "judge.py", "analyze.py", "config/models.json", "config/pricing.json",
           "datasets/router_taskb.jsonl", "scripts/build_router_dataset.py")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--run", default=RUN)
    parser.add_argument("--allow-partial", action="store_true",
                        help="export whatever exists (for interim analysis); the archive is marked partial")
    args = parser.parse_args()

    performance = ROOT / "outputs" / f"router_{args.run}.jsonl"
    quality = ROOT / "outputs" / f"quality_{args.run}.jsonl"
    raw = performance.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    quality_bytes = quality.read_bytes() if quality.exists() else b""
    scores = [json.loads(line) for line in quality_bytes.splitlines() if line.strip()]
    complete = len(rows) == EXPECTED_ROWS and len(scores) == EXPECTED_SCORES
    if not complete and not args.allow_partial:
        raise ValueError(f"Run incomplete: {len(rows)}/{EXPECTED_ROWS} rows, {len(scores)}/{EXPECTED_SCORES} scores.")

    numerical = []
    for row in rows:
        text = row.get("response_text") or ""
        numerical.append({
            **{k: v for k, v in row.items() if k not in OMITTED},
            "response_sha256": sha256(text.encode()),
        })
    columns = sorted({key for row in numerical for key in row})

    request = Request("http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01",
                      headers={"Metadata": "true"})
    with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
        metadata = json.load(response)

    evidence = {
        "schema_version": 2,
        "task": "B-model-router",
        "run_id": args.run,
        "complete": complete,
        "performance_columns": columns,
        "performance_values": [[row.get(c) for c in columns] for row in numerical],
        "quality": scores,
        "provenance": {
            "raw_file": str(performance),
            "raw_sha256": sha256(raw),
            "raw_bytes": len(raw),
            "raw_rows": len(rows),
            "quality_file": str(quality) if quality.exists() else None,
            "quality_sha256": sha256(quality_bytes) if quality.exists() else None,
            "quality_rows": len(scores),
            "omitted_fields": list(OMITTED),
            "vm_metadata": {k: metadata[k] for k in ("name", "location", "vmSize", "osType")},
            "python": platform.python_version(),
            "openai": importlib.metadata.version("openai"),
            "azure_identity": importlib.metadata.version("azure-identity"),
            "source_sha256": {name: sha256((ROOT / name).read_bytes()) for name in SOURCES if (ROOT / name).exists()},
        },
    }
    suffix = "" if complete else "_partial"
    target = ROOT / "outputs" / f"evidence_router_{args.run}{suffix}.json.xz"
    payload = lzma.compress(json.dumps(evidence, separators=(",", ":")).encode())
    target.write_bytes(payload)
    print(json.dumps({"path": str(target), "bytes": len(payload), "sha256": sha256(payload),
                      "rows": len(rows), "scores": len(scores), "complete": complete}))


if __name__ == "__main__":
    main()
