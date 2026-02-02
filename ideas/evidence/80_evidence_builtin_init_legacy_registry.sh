#!/usr/bin/env bash
set -euo pipefail

echo '## Remove legacy ToolRegistry wiring in builtin tools init.'

echo '\n$ rg -n 'legacy' apps/zerg/backend/zerg/tools/builtin/__init__.py'
rg -n 'legacy' apps/zerg/backend/zerg/tools/builtin/__init__.py
