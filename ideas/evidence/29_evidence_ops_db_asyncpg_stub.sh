#!/usr/bin/env bash
set -euo pipefail

echo '## Remove asyncpg stub ops_db module that raises NotImplemented.'

echo '\n$ sed -n '1,80p' apps/zerg/backend/zerg/jobs/ops_db.py'
sed -n '1,80p' apps/zerg/backend/zerg/jobs/ops_db.py
