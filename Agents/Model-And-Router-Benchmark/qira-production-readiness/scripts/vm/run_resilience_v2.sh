#!/bin/bash
cd /home/azureuser/bench
export AZURE_OPENAI_ENDPOINT=https://YOUR-ENDPOINT.cognitiveservices.azure.com
export PYTHONUNBUFFERED=1
PY=/home/azureuser/bench/.venv/bin/python
echo "resilience v2 start $(date -u +%FT%TZ)"
$PY resilience_test.py --dataset datasets/qira_scenarios.jsonl \
  --primary gpt-5.6-luna-lowcap --fallback gpt-5.6-luna --effort none --api responses \
  --policies none,reactive,proactive --threshold 0.8 --level 8 --duration 75 --ramp 15 --settle-seconds 70 \
  --region swedencentral --client-location swedencentral-linux-vm > prodready_resilience_v2.log 2>&1
echo "resilience v2 finished $(date -u +%FT%TZ) rc=$?"
