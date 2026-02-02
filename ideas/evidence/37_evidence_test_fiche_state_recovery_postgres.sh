#!/usr/bin/env bash
set -euo pipefail

echo '## Remove advisory-lock support tests after SQLite-only pivot.'

echo '\n$ rg -n 'advisory' apps/zerg/backend/tests/test_fiche_state_recovery.py'
rg -n 'advisory' apps/zerg/backend/tests/test_fiche_state_recovery.py
