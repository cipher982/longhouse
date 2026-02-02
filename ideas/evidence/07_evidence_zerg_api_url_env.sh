#!/usr/bin/env bash
set -euo pipefail

echo '## Rename ZERG_API_URL env var to LONGHOUSE_API_URL and drop fallback.'

echo '\n$ rg -n 'ZERG_API_URL' apps/zerg/backend -g '*.py''
rg -n 'ZERG_API_URL' apps/zerg/backend -g '*.py'
