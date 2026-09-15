#!/usr/bin/env python3
"""
Discover which reasoning_effort values each deployment actually accepts.

The supported set differs by model family and changes between releases, so this
probes the live endpoint instead of trusting a hardcoded table. Writes the
result into config/models.json as `supported_efforts`, which harness.py then
expands into the full model x effort matrix.

Usage:
  python probe_efforts.py --deployments gpt-5.6-luna,gpt-5-mini,gpt-4o-mini-bench
  python probe_efforts.py --deployments ... --write

Author: Xinyu Wei (魏新宇)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import harness

ROOT = Path(__file__).resolve().parent
MODELS_PATH = ROOT / "config" / "models.json"

# Every value seen across the GPT-5.x families; the probe decides which are real.
CANDIDATES = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]

PROBE_PROMPT = "Reply with the single word: ok"


def probe(client, deployment: str, effort: str | None) -> tuple[bool, str]:
    kwargs = {
        "model": deployment,
        "input": [{"role": "user", "content": PROBE_PROMPT}],
        "max_output_tokens": 2000,
    }
    if effort is not None:
        kwargs["reasoning"] = {"effort": effort}
    try:
        client.responses.create(**kwargs)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        # Distinguish "this value is invalid" from "the whole call failed".
        return False, msg[:200]


def main() -> int:
    p = argparse.ArgumentParser(description="Probe supported reasoning_effort values.")
    p.add_argument("--deployments", required=True, help="comma-separated deployment names")
    p.add_argument("--write", action="store_true",
                   help="write discovered values into config/models.json")
    p.add_argument("--models", default=str(MODELS_PATH))
    args = p.parse_args()

    targets = [d.strip() for d in args.deployments.split(",") if d.strip()]
    client, endpoint = harness.build_client()
    print(f"endpoint: {endpoint}\n")

    discovered: dict[str, dict] = {}

    for dep in targets:
        print(f"=== {dep} ===")
        baseline_ok, baseline_err = probe(client, dep, None)
        if not baseline_ok:
            print(f"  [FAIL] deployment unreachable without reasoning: {baseline_err}")
            print()
            continue
        print(f"  {'(no effort sent)':<20} OK")

        supported = []
        for effort in CANDIDATES:
            ok, err = probe(client, dep, effort)
            if ok:
                supported.append(effort)
                print(f"  effort={effort:<14} OK")
            else:
                short = err.split("Message:")[-1].strip()[:90] if "Message:" in err else err[:90]
                print(f"  effort={effort:<14} rejected — {short}")

        discovered[dep] = {
            "supports_reasoning": bool(supported),
            "supported_efforts": supported,
        }
        print(f"  → supported: {supported or '(none — non-reasoning model)'}")
        print()

    if not args.write:
        print("Re-run with --write to persist into config/models.json")
        return 0

    path = Path(args.models)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"models": {}}
    data.setdefault("models", {})
    for dep, info in discovered.items():
        entry = data["models"].get(dep, {})
        entry["family"] = "reasoning" if info["supports_reasoning"] else "non-reasoning"
        entry["supported_efforts"] = info["supported_efforts"]
        entry.setdefault("reasoning_effort", info["supported_efforts"][0]
                         if info["supported_efforts"] else None)
        entry["probed"] = True
        data["models"][dep] = entry
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
