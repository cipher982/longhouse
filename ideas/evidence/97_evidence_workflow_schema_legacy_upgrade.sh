#!/usr/bin/env bash
set -euo pipefail

echo '## Remove legacy trigger upgrade logic in schemas/workflow.py.'

echo '\n$ rg -n 'legacy' apps/zerg/backend/zerg/schemas/workflow.py'
rg -n 'legacy' apps/zerg/backend/zerg/schemas/workflow.py
