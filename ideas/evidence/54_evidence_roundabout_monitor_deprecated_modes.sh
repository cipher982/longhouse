#!/usr/bin/env bash
set -euo pipefail

echo '## Remove deprecated heuristic or hybrid decision modes in roundabout monitor.'

echo '\n$ rg -n 'DEPRECATED' apps/zerg/backend/zerg/services/roundabout_monitor.py'
rg -n 'DEPRECATED' apps/zerg/backend/zerg/services/roundabout_monitor.py
