#!/usr/bin/env bash
set -euo pipefail

echo '## Replace api.zerg.ai references with longhouse.ai branding.'

echo '\n$ rg -n 'zerg.ai' apps/zerg/backend/zerg/main.py'
rg -n 'zerg.ai' apps/zerg/backend/zerg/main.py
