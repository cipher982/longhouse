#!/usr/bin/env bash
set -euo pipefail

echo '## Split fiche_runner.py (large file).'

echo '\n$ wc -l apps/zerg/backend/zerg/managers/fiche_runner.py'
wc -l apps/zerg/backend/zerg/managers/fiche_runner.py
