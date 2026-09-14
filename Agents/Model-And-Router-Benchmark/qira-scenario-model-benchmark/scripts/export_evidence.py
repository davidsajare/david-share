"""Prepare a compact, checksummed numerical export without opening network ports.

Runs on the benchmark VM: strips response text, attaches IMDS VM metadata,
compresses the archive and records SHA256 values for the provenance file.

Author: Xinyu Wei (魏新宇)
"""

import hashlib
import importlib.metadata
import json
import lzma
import platform
from pathlib import Path
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
RUN = "20260909_120534"


def main():
    performance = ROOT / "outputs" / f"direct_{RUN}.jsonl"
    quality = ROOT / "outputs" / f"quality_fulltext_{RUN}.jsonl"
    raw = performance.read_bytes()
    quality_bytes = quality.read_bytes()
    rows = [json.loads(line) for line in raw.splitlines()]
    scores = [json.loads(line) for line in quality_bytes.splitlines()]
    if len(rows) != 748 or len(scores) != 187:
        raise ValueError("Matrix or full-text quality repair is incomplete.")
    numerical = []
    for row in rows:
        numerical.append({
            **{k: v for k, v in row.items() if k not in ("response_text", "response_preview")},
            "response_sha256": hashlib.sha256(row["response_text"].encode()).hexdigest(),
        })
    columns = sorted(numerical[0])
    request = Request(
        "http://169.254.169.254/metadata/instance/compute?api-version=2021-02-01",
        headers={"Metadata": "true"},
    )
    with build_opener(ProxyHandler({})).open(request, timeout=5) as response:
        metadata = json.load(response)
    evidence = {
        "schema_version": 1,
        "run_id": RUN,
        "performance_columns": columns,
        "performance_values": [[row.get(c) for c in columns] for row in numerical],
        "quality": scores,
        "provenance": {
            "raw_file": str(performance),
            "raw_sha256": hashlib.sha256(raw).hexdigest(),
            "raw_bytes": len(raw),
            "quality_file": str(quality),
            "quality_sha256": hashlib.sha256(quality_bytes).hexdigest(),
            "omitted_fields": ["response_text", "response_preview"],
            "vm_metadata": {k: metadata[k] for k in ("name", "location", "vmSize", "osType")},
            "python": platform.python_version(),
            "openai": importlib.metadata.version("openai"),
            "azure_identity": importlib.metadata.version("azure-identity"),
            "source_sha256": {
                str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (ROOT / "harness.py", ROOT / "datasets" / "qira_scenarios.jsonl")
            },
        },
    }
    target = ROOT / "outputs" / f"evidence_{RUN}.json.xz"
    payload = lzma.compress(json.dumps(evidence, separators=(",", ":")).encode())
    target.write_bytes(payload)
    print(json.dumps({"path": str(target), "bytes": len(payload),
                      "sha256": hashlib.sha256(payload).hexdigest()}))


if __name__ == "__main__":
    main()
