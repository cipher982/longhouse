#!/usr/bin/env bash
set -euo pipefail

echo '## Remove agents schema mapping for SQLite-only core.'

echo '\n$ rg -n 'schema' apps/zerg/backend/zerg/models/agents.py'
rg -n 'schema' apps/zerg/backend/zerg/models/agents.py
