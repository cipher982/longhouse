#!/usr/bin/env bash
set -euo pipefail

echo '## Split oikos_tools.py (large file).'

echo '\n$ wc -l apps/zerg/backend/zerg/tools/builtin/oikos_tools.py'
wc -l apps/zerg/backend/zerg/tools/builtin/oikos_tools.py
