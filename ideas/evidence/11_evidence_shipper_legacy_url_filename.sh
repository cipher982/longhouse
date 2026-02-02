#!/usr/bin/env bash
set -euo pipefail

echo '## Drop legacy url filename zerg-url after migration.'

echo '\n$ rg -n 'zerg-url' apps/zerg/backend/zerg/services/shipper/token.py'
rg -n 'zerg-url' apps/zerg/backend/zerg/services/shipper/token.py
