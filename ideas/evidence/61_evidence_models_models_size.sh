#!/usr/bin/env bash
set -euo pipefail

echo '## Split models/models.py (large file).'

echo '\n$ wc -l apps/zerg/backend/zerg/models/models.py'
wc -l apps/zerg/backend/zerg/models/models.py
