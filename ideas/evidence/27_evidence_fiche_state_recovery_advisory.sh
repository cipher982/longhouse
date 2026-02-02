#!/usr/bin/env bash
set -euo pipefail

echo '## Remove Postgres advisory lock support from fiche_state_recovery.'

echo '\n$ rg -n 'advisory' apps/zerg/backend/zerg/services/fiche_state_recovery.py'
rg -n 'advisory' apps/zerg/backend/zerg/services/fiche_state_recovery.py
