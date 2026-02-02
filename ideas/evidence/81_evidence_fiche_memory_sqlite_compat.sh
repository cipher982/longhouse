#!/usr/bin/env bash
set -euo pipefail

echo '## Simplify fiche_memory_tools SQLite compatibility filtering with better schema or indexes.'

echo '\n$ rg -n 'SQLite' apps/zerg/backend/zerg/tools/builtin/fiche_memory_tools.py'
rg -n 'SQLite' apps/zerg/backend/zerg/tools/builtin/fiche_memory_tools.py
