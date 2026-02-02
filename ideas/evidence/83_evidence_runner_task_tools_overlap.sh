#!/usr/bin/env bash
set -euo pipefail

echo '## Consolidate runner_tools and task_tools overlap.'

echo '\n$ rg --files apps/zerg/backend/zerg/tools/builtin -g '*runner_tools.py' -g '*task_tools.py''
rg --files apps/zerg/backend/zerg/tools/builtin -g '*runner_tools.py' -g '*task_tools.py'
