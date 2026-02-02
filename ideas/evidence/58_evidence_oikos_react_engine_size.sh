#!/usr/bin/env bash
set -euo pipefail

echo '## Split oikos_react_engine.py (large file).'

echo '\n$ wc -l apps/zerg/backend/zerg/services/oikos_react_engine.py'
wc -l apps/zerg/backend/zerg/services/oikos_react_engine.py
