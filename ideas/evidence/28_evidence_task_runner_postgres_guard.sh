#!/usr/bin/env bash
set -euo pipefail

echo '## Simplify task_runner Postgres guard logic in SQLite-only mode.'

echo '\n$ rg -n 'postgresql' apps/zerg/backend/zerg/services/task_runner.py'
rg -n 'postgresql' apps/zerg/backend/zerg/services/task_runner.py
