"""Query the Foundry Models API for lifecycle status, retirement dates, SKUs and API capabilities.

The customer asked for "lifecycle and compatibility" alongside quality/latency/cost. Those are
facts published by the service, not measurements, so this script records them from the
management API for the exact model versions used in the studies and writes
outputs/model_lifecycle_<region>.json with the query timestamp.

Usage:
    python scripts/query_model_lifecycle.py --region swedencentral

Requires Azure CLI login with reader access to the subscription's Cognitive Services provider.

Author: Xinyu Wei (魏新宇)
"""

import argparse
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = {
    ("gpt-4o-mini", "2024-07-18"): "Task A candidate (gpt-4o-mini-bench)",
    ("gpt-5-mini", "2025-08-07"): "Task A candidate",
    ("gpt-5.6-luna", "2026-07-09"): "Task A candidate; Task B fast tier; fallback target",
    ("gpt-5.6-sol", "2026-07-09"): "Task B strong tier",
    ("gpt-5.6-terra", "2026-07-09"): "blind judge (judge-terra)",
    ("model-router", "2025-11-18"): "Task B router deployments",
}
CAPABILITY_KEYS = ("chatCompletion", "responses", "assistants", "jsonObjectResponse", "jsonSchemaResponse",
                   "fineTune", "maxContextToken", "maxOutputToken", "agentsV2", "realtime", "audio")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--region", default="swedencentral")
    args = parser.parse_args()
    az = shutil.which("az")
    if az is None:
        raise SystemExit("Azure CLI is required.")
    result = subprocess.run([az, "cognitiveservices", "model", "list", "-l", args.region, "-o", "json", "--only-show-errors"],
                            check=True, capture_output=True, text=True, encoding="utf-8")
    models = json.loads(result.stdout)
    rows, seen = [], set()
    for entry in models:
        m = entry.get("model") or {}
        key = (m.get("name"), m.get("version"))
        if key not in VERSIONS or m.get("format") != "OpenAI" or key in seen:
            continue
        seen.add(key)
        deprecation = m.get("deprecation") or {}
        caps = m.get("capabilities") or {}
        rows.append({
            "model": key[0], "version": key[1], "role_in_studies": VERSIONS[key],
            "lifecycle_status_api": m.get("lifecycleStatus"),
            "lifecycle_status_docs_meaning": {"GenerallyAvailable": "GA", "Deprecating": "Deprecated (existing customers only)",
                                              "Deprecated": "Retired", "Preview": "Preview"}.get(m.get("lifecycleStatus"), m.get("lifecycleStatus")),
            "inference_retirement_utc": deprecation.get("inference"),
            "finetune_retirement_utc": deprecation.get("fineTune"),
            "skus": sorted({s["name"] for s in (m.get("skus") or [])}),
            "capabilities": {k: caps.get(k) for k in CAPABILITY_KEYS if k in caps},
        })
    missing = [f"{n} {v}" for (n, v) in VERSIONS if (n, v) not in seen]
    if missing:
        raise SystemExit(f"Models API did not return: {missing}")
    output = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "region": args.region,
        "source": "az cognitiveservices model list (Microsoft.CognitiveServices models API)",
        "status_mapping_source": "https://learn.microsoft.com/en-us/azure/foundry/openai/concepts/model-retirements",
        "note": "lifecycleStatus 'Deprecating' means deprecated for new customers but still serving existing subscriptions; "
                "'Deprecated' would mean retired (410). Dates are the service's programmatic values on the query date and can move.",
        "models": sorted(rows, key=lambda r: (r["inference_retirement_utc"] or "", r["model"])),
    }
    target = ROOT / "outputs" / f"model_lifecycle_{args.region}.json"
    target.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8", newline="\n")
    for r in output["models"]:
        print(f"{r['model']:<14}{r['version']:<12}{r['lifecycle_status_api']:<20}retires {str(r['inference_retirement_utc'])[:10]}  skus={len(r['skus'])}  caps={','.join(k for k, v in r['capabilities'].items() if v == 'true')}")
    print(f"wrote {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
