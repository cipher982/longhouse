#!/usr/bin/env bash
set -euo pipefail

echo '## Update tool docstrings referencing Life Hub session resume.'

echo '\n$ rg -n 'Life Hub' apps/zerg/backend/zerg/tools/builtin/oikos_tools.py'
rg -n 'Life Hub' apps/zerg/backend/zerg/tools/builtin/oikos_tools.py
