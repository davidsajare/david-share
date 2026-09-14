"""Record observed Azure state and retained evidence hashes after the follow-up runs.

Read-only against Azure: fails unless the VM is deallocated and no temporary SSH
rule remains; writes outputs/resource_closeout_followup.json. No resource
identifiers are written to the report.

Author: Xinyu Wei (魏新宇)
"""

import argparse
import hashlib
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def azure_json(*args):
    az = shutil.which("az")
    if az is None:
        raise RuntimeError("Azure CLI is required to observe resource state.")
    result = subprocess.run([az, *args, "--output", "json", "--only-show-errors"],
                            check=True, capture_output=True, text=True, encoding="utf-8")
    return json.loads(result.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--resource-group", required=True)
    parser.add_argument("--vm", required=True)
    parser.add_argument("--nsg", action="append", required=True)
    args = parser.parse_args()
    vm = azure_json("vm", "get-instance-view", "-g", args.resource_group, "-n", args.vm)
    states = {s["code"] for s in vm["instanceView"]["statuses"]}
    if "PowerState/deallocated" not in states:
        raise ValueError(f"VM is not deallocated: {states}")
    for nsg in args.nsg:
        rules = azure_json("network", "nsg", "rule", "list", "-g", args.resource_group, "--nsg-name", nsg)
        if any(rule["name"] == "tmp-ssh" for rule in rules):
            raise ValueError("A temporary SSH rule remains.")
    provenance = json.loads((ROOT / "outputs" / "provenance_followup.json").read_text(encoding="utf-8"))
    retained = {}
    for rel in provenance["sha256"]:
        if rel.startswith("outputs/raw_fulltext/") or rel.startswith("outputs/quality_v2_"):
            path = ROOT / rel
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != provenance["sha256"][rel]:
                raise ValueError(f"{rel} changed since the provenance was written.")
            retained[rel] = digest
    closeout = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "runs": provenance["runs"],
        "vm_power_state": "PowerState/deallocated",
        "vm_deleted": False,
        "vm_region": vm["location"],
        "temporary_ssh_rules_remaining": False,
        "nsgs_checked": len(args.nsg),
        "vm_disk_is_sole_copy": False,
        "model_deployments": "retained; no deletion requested or performed",
        "retained_sha256": retained,
    }
    target = ROOT / "outputs" / "resource_closeout_followup.json"
    target.write_text(json.dumps(closeout, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(json.dumps(closeout, indent=2))


if __name__ == "__main__":
    main()
