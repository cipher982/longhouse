#!/usr/bin/env bash
set -euo pipefail

echo '## Simplify jobs registry import graph to a single jobs pack.'

echo '\n$ rg -n 'import' apps/zerg/backend/zerg/jobs/registry.py'
rg -n 'import' apps/zerg/backend/zerg/jobs/registry.py
