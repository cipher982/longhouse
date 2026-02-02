#!/usr/bin/env bash
set -euo pipefail

echo '## Move container_tools out of core if containerized commis is optional.'

echo '\n$ rg --files apps/zerg/backend/zerg/tools/builtin -g '*container_tools.py''
rg --files apps/zerg/backend/zerg/tools/builtin -g '*container_tools.py'
