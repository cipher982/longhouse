#!/usr/bin/env bash
set -euo pipefail

echo '## Remove legacy admin routes without api prefix.'

echo '\n$ rg -n '_legacy' apps/zerg/backend/zerg/routers/admin.py'
rg -n '_legacy' apps/zerg/backend/zerg/routers/admin.py
