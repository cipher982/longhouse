#!/usr/bin/env bash
set -euo pipefail

echo '## Move Postgres checkpointer to optional module.'

echo '\n$ rg -n 'Postgres' apps/zerg/backend/zerg/services/checkpointer.py'
rg -n 'Postgres' apps/zerg/backend/zerg/services/checkpointer.py
echo '\n$ rg -n 'postgresql' apps/zerg/backend/zerg/services/checkpointer.py'
rg -n 'postgresql' apps/zerg/backend/zerg/services/checkpointer.py
echo '\n$ rg -n 'psycopg' apps/zerg/backend/zerg/services/checkpointer.py'
rg -n 'psycopg' apps/zerg/backend/zerg/services/checkpointer.py
