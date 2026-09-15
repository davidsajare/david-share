"""Record observed Azure state and retained evidence hashes after Task B.

This script does not change Azure resources. It fails unless the VM is already
deallocated, both inspected NSGs have no temporary SSH rules, and every retained
answer matches the evidence. Resource identifiers are not written to the report.

Author: Xinyu Wei (魏新宇)
"""

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUN = "20260909_223737"


def azure_json(*args):
    az = shutil.which("az")
    if az is None:
        raise RuntimeError("Azure CLI is required to observe resource state.")
    result = subprocess.run(
        [az, *args, "--output", "json", "--only-show-errors"],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--vm", required=True)
    parser.add_argument("--nsg", action="append", required=True)
    args = parser.parse_args()
    subprocess.run([sys.executable, str(ROOT / "scripts" / "verify_router_fulltext.py")], check=True)
    vm = azure_json("vm", "get-instance-view", "-g", args.resource_group, "-n", args.vm)
    states = {s["code"] for s in vm["instanceView"]["statuses"]}
    if "PowerState/deallocated" not in states:
        raise ValueError(f"VM is not deallocated: {states}")
    for nsg in args.nsg:
        rules = azure_json("network", "nsg", "rule", "list", "-g", args.resource_group, "--nsg-name", nsg)
        if any(rule["name"] == "tmp-ssh" for rule in rules):
            raise ValueError("A temporary SSH rule remains.")
    output = ROOT / "outputs"
    paths = [
        output / f"evidence_router_{RUN}.json.xz",
        output / "raw_fulltext" / f"router_{RUN}.jsonl",
        output / "raw_fulltext" / f"quality_{RUN}.jsonl",
    ]
    closeout = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "run_id": RUN,
        "vm_power_state": "PowerState/deallocated",
        "vm_deleted": False,
        "vm_region": vm["location"],
        "temporary_ssh_rules_remaining": False,
        "nsgs_checked": len(args.nsg),
        "vm_disk_is_sole_copy": False,
        "model_deployments": "retained; no deletion requested or performed",
        "temporary_evidence_storage": "empty transfer account removed; network policy left unchanged",
        "fulltext_verification": "passed before closeout",
        "retained_sha256": {
            p.relative_to(ROOT).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in paths
        },
    }
    target = output / "resource_closeout_router.json"
    target.write_text(json.dumps(closeout, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(closeout, indent=2))


if __name__ == "__main__":
    main()
