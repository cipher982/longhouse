#!/usr/bin/env bash
set -euo pipefail

echo '## Split oikos_service.py (large file).'

echo '\n$ wc -l apps/zerg/backend/zerg/services/oikos_service.py'
wc -l apps/zerg/backend/zerg/services/oikos_service.py
