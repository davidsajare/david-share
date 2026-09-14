#!/bin/bash
cd /home/azureuser/bench
export AZURE_OPENAI_ENDPOINT=https://YOUR-ENDPOINT.cognitiveservices.azure.com
export PYTHONUNBUFFERED=1
PY=/home/azureuser/bench/.venv/bin/python
echo "prodready start $(date -u +%FT%TZ)"
$PY sessions.py --dataset datasets/qira_sessions.jsonl \
  --arms "gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat,router-sol-luna-quality#chat" \
  --iterations 3 --region swedencentral --client-location swedencentral-linux-vm > prodready_sessions.log 2>&1
echo "sessions finished $(date -u +%FT%TZ) rc=$?"
$PY sustained_load.py --dataset datasets/qira_scenarios.jsonl \
  --arms "gpt-4o-mini-bench,gpt-5-mini@minimal,gpt-5.6-luna@none,router-sol-luna-balanced#chat" \
  --levels 4,8,16 --duration 90 --ramp 15 --settle-seconds 15 --region swedencentral \
  --client-location swedencentral-linux-vm > prodready_sustained.log 2>&1
echo "sustained finished $(date -u +%FT%TZ) rc=$?"
$PY resilience_test.py --dataset datasets/qira_scenarios.jsonl \
  --primary gpt-5.6-luna-lowcap --fallback gpt-5.6-luna --effort none --api responses \
  --policies none,reactive,proactive --threshold 0.8 --level 8 --duration 75 --ramp 15 --settle-seconds 70 \
  --region swedencentral --client-location swedencentral-linux-vm > prodready_resilience.log 2>&1
echo "resilience finished $(date -u +%FT%TZ) rc=$?"
echo "prodready done $(date -u +%FT%TZ)"
