#!/usr/bin/env bash
set -euo pipefail

echo '## Drop legacy token filename zerg-device-token after migration.'

echo '\n$ rg -n 'zerg-device-token' apps/zerg/backend/zerg/services/shipper/token.py'
rg -n 'zerg-device-token' apps/zerg/backend/zerg/services/shipper/token.py
