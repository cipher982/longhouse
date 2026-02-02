#!/usr/bin/env bash
set -euo pipefail

echo '## Replace postgresql.UUID or JSONB in agents schema migration.'

echo '\n$ rg -n 'postgresql' apps/zerg/backend/alembic/versions/0002_agents_schema.py'
rg -n 'postgresql' apps/zerg/backend/alembic/versions/0002_agents_schema.py
echo '\n$ rg -n 'No Postgres' VISION.md'
rg -n 'No Postgres' VISION.md
