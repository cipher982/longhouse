#!/usr/bin/env bash
set -euo pipefail

echo '## Split routers/agents.py (large file).'

echo '\n$ wc -l apps/zerg/backend/zerg/routers/agents.py'
wc -l apps/zerg/backend/zerg/routers/agents.py
