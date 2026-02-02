#!/usr/bin/env bash
set -euo pipefail

echo '## Remove life_hub imports in jobs registry.'

echo '\n$ rg -n 'life_hub' apps/zerg/backend/zerg/jobs/registry.py'
rg -n 'life_hub' apps/zerg/backend/zerg/jobs/registry.py
