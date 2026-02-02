#!/usr/bin/env bash
set -euo pipefail

echo '## Split roundabout_monitor.py (large file).'

echo '\n$ wc -l apps/zerg/backend/zerg/services/roundabout_monitor.py'
wc -l apps/zerg/backend/zerg/services/roundabout_monitor.py
