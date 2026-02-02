#!/usr/bin/env bash
set -euo pipefail

echo '## Split commis_runner.py (large file).'

echo '\n$ wc -l apps/zerg/backend/zerg/services/commis_runner.py'
wc -l apps/zerg/backend/zerg/services/commis_runner.py
