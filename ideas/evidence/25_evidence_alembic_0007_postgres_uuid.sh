#!/usr/bin/env bash
set -euo pipefail

echo '## Replace postgresql.UUID in memories migration.'

echo '\n$ rg -n 'postgresql' apps/zerg/backend/alembic/versions/0007_add_memories_table.py'
rg -n 'postgresql' apps/zerg/backend/alembic/versions/0007_add_memories_table.py
