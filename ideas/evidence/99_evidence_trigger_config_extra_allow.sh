#!/usr/bin/env bash
set -euo pipefail

echo '## Tighten trigger_config schema by removing extra allow compatibility.'

echo '\n$ rg -n 'extra' apps/zerg/backend/zerg/models/trigger_config.py'
rg -n 'extra' apps/zerg/backend/zerg/models/trigger_config.py
