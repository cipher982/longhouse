#!/usr/bin/env bash
set -euo pipefail

echo '## Remove deprecated workflow start route.'

echo '\n$ rg -n 'deprecated' apps/zerg/backend/zerg/routers/workflow_executions.py'
rg -n 'deprecated' apps/zerg/backend/zerg/routers/workflow_executions.py
