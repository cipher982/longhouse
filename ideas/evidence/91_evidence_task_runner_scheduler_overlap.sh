#!/usr/bin/env bash
set -euo pipefail

echo '## Evaluate merging task_runner with scheduler service.'

echo '\n$ rg -n 'SchedulerService' apps/zerg/backend/zerg/services/task_runner.py'
rg -n 'SchedulerService' apps/zerg/backend/zerg/services/task_runner.py
