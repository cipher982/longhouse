#!/usr/bin/env bash
set -euo pipefail

echo '## Rename SwarmOpsPage to Runs or remove Swarm naming not in VISION.'

echo '\n$ rg -n 'SwarmOpsPage' apps/zerg/frontend-web/src/routes/App.tsx'
rg -n 'SwarmOpsPage' apps/zerg/frontend-web/src/routes/App.tsx
