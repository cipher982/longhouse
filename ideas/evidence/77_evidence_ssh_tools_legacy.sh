#!/usr/bin/env bash
set -euo pipefail

echo '## Move or remove legacy ssh_tools from core.'

echo '\n$ rg -n 'legacy' apps/zerg/backend/zerg/tools/builtin/ssh_tools.py'
rg -n 'legacy' apps/zerg/backend/zerg/tools/builtin/ssh_tools.py
