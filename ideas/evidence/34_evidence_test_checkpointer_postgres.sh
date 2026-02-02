#!/usr/bin/env bash
set -euo pipefail

echo '## Move Postgres-only checkpointer tests out of default suite.'

echo '\n$ rg -n 'postgresql://' apps/zerg/backend/tests/test_checkpointer.py'
rg -n 'postgresql://' apps/zerg/backend/tests/test_checkpointer.py
