#!/usr/bin/env bash
set -euo pipefail

echo '## Drop ensure_agents_schema Postgres-only schema creation.'

echo '\n$ rg -n 'CREATE SCHEMA' apps/zerg/backend/zerg/services/agents_store.py'
rg -n 'CREATE SCHEMA' apps/zerg/backend/zerg/services/agents_store.py
