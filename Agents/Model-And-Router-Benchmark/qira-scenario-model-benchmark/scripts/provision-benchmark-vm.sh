#!/usr/bin/env bash
# Provision a Linux load-generation VM in the SAME region as the Azure OpenAI
# resource, so that network RTT stops contaminating the TTFT comparison.
#
# Why this exists: TTFT bundles network round-trip + queuing + prefill + first
# token. Measured from East Asia to East US 2, the network alone contributes
# ~100-200ms (AOAI-Model-Migration-Benchmark, "TTFT Composition") - larger than
# the differences between the models being compared. Running from a VM inside
# the same region drops that to low single-digit milliseconds.
#
# Usage:
#   ./provision-benchmark-vm.sh <resource-group> <region> [vm-name]
#
# Example:
#   ./provision-benchmark-vm.sh qira-benchmark-rg eastus2

set -euo pipefail

RG="${1:?usage: $0 <resource-group> <region> [vm-name]}"
REGION="${2:?usage: $0 <resource-group> <region> [vm-name]}"
VM_NAME="${3:-qira-bench-vm}"

VM_SIZE="${VM_SIZE:-Standard_D4s_v5}"
IMAGE="${IMAGE:-Ubuntu2204}"
ADMIN_USER="${ADMIN_USER:-azureuser}"

echo "resource group : $RG"
echo "region         : $REGION   <-- must match the Azure OpenAI resource region"
echo "vm             : $VM_NAME ($VM_SIZE, $IMAGE)"
echo

az group create --name "$RG" --location "$REGION" --output none

az vm create \
  --resource-group "$RG" \
  --name "$VM_NAME" \
  --location "$REGION" \
  --image "$IMAGE" \
  --size "$VM_SIZE" \
  --admin-username "$ADMIN_USER" \
  --generate-ssh-keys \
  --public-ip-sku Standard \
  --output none

PUBLIC_IP="$(az vm show -d -g "$RG" -n "$VM_NAME" --query publicIps -o tsv)"

cat <<EOF

VM ready.

  ssh $ADMIN_USER@$PUBLIC_IP

Next, on the VM:

  sudo apt-get update && sudo apt-get install -y python3-venv python3-pip
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt

  export AZURE_OPENAI_ENDPOINT="https://<your-resource>.openai.azure.com"
  export AZURE_OPENAI_API_KEY="<your-key>"

Confirm the network floor before benchmarking - this must read same-region:

  python3 harness.py --mode direct \\
      --dataset datasets/qira_scenarios.jsonl \\
      --deployments gpt-4o-mini \\
      --region $REGION --client-location $REGION-linux-vm --preflight

If the reported RTT is not in the low single-digit milliseconds, the VM is not
actually co-located with the Azure OpenAI resource. Check the resource's region
before trusting any latency number.

Tear down when finished:

  az group delete --name $RG --yes --no-wait

EOF
