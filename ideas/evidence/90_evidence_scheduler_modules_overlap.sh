#!/usr/bin/env bash
set -euo pipefail

echo '## Consolidate scheduler_service.py and workflow_scheduler.py.'

echo '\n$ rg --files apps/zerg/backend/zerg/services -g '*scheduler*.py''
rg --files apps/zerg/backend/zerg/services -g '*scheduler*.py'
