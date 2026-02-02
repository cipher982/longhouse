#!/usr/bin/env bash
set -euo pipefail

echo '## Remove or merge DashboardPage since VISION says timeline is primary user landing.'

echo '\n$ rg -n 'Timeline' VISION.md'
rg -n 'Timeline' VISION.md
echo '\n$ rg -n 'DashboardPage' apps/zerg/frontend-web/src/routes/App.tsx'
rg -n 'DashboardPage' apps/zerg/frontend-web/src/routes/App.tsx
