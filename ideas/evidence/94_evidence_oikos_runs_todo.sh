#!/usr/bin/env bash
set -euo pipefail

echo '## Remove TODO in oikos_runs router by implementing filter in CRUD.'

echo '\n$ rg -n 'TODO' apps/zerg/backend/zerg/routers/oikos_runs.py'
rg -n 'TODO' apps/zerg/backend/zerg/routers/oikos_runs.py
