#!/usr/bin/env bash
set -euo pipefail

echo '## Split main.py (large file).'

echo '\n$ wc -l apps/zerg/backend/zerg/main.py'
wc -l apps/zerg/backend/zerg/main.py
