#!/usr/bin/env bash
set -euo pipefail

echo '## Remove deprecated trigger_type field in workflow_schema.py.'

echo '\n$ rg -n 'deprecated' apps/zerg/backend/zerg/schemas/workflow_schema.py'
rg -n 'deprecated' apps/zerg/backend/zerg/schemas/workflow_schema.py
