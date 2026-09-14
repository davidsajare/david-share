#!/usr/bin/env bash
# Start the live console on the benchmark VM.
#
# Run this on the same-region Linux VM the studies used, not on a laptop:
# a laptop adds its own network latency to every TTFT measurement, which is
# the one thing these charts must not be confused about.
#
#   ./run_on_vm.sh [port]
#
# The console binds to 127.0.0.1 by default. To reach it from your laptop,
# forward the port over SSH rather than opening it to the internet:
#
#   ssh -L 8080:127.0.0.1:8080 <user>@<vm-ip>
#
# then open http://localhost:8080/ locally. No inbound NSG rule is needed.

set -euo pipefail

PORT="${1:-8080}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -z "${AZURE_OPENAI_ENDPOINT:-}" ]]; then
  echo "AZURE_OPENAI_ENDPOINT is not set; the console will start in replay mode." >&2
  echo "Copy .env.example to .env and fill it in to run live." >&2
fi

PYTHON="${PYTHON:-python3}"
if [[ -d .venv ]]; then
  PYTHON=".venv/bin/python"
fi

exec "$PYTHON" server.py --host 127.0.0.1 --port "$PORT"
