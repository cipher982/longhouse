#!/usr/bin/env bash
set -euo pipefail

echo '## Simplify unified_access legacy behavior.'

echo '\n$ rg -n 'legacy' apps/zerg/backend/zerg/tools/unified_access.py'
rg -n 'legacy' apps/zerg/backend/zerg/tools/unified_access.py
