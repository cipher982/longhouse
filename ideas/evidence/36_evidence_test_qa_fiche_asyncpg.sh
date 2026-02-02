#!/usr/bin/env bash
set -euo pipefail

echo '## Remove asyncpg result handling tests once asyncpg removed.'

echo '\n$ rg -n 'asyncpg' apps/zerg/backend/tests/jobs/test_qa_fiche.py'
rg -n 'asyncpg' apps/zerg/backend/tests/jobs/test_qa_fiche.py
