#!/usr/bin/env bash
set -euo pipefail

echo '## Rename default runner image from zerg-runner to longhouse-runner.'

echo '\n$ rg -n 'zerg-runner' apps/zerg/backend/zerg/config/__init__.py'
rg -n 'zerg-runner' apps/zerg/backend/zerg/config/__init__.py
