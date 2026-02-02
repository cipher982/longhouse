#!/usr/bin/env bash
set -euo pipefail

echo '## Split or remove DashboardPage.tsx (large file).'

echo '\n$ wc -l apps/zerg/frontend-web/src/pages/DashboardPage.tsx'
wc -l apps/zerg/frontend-web/src/pages/DashboardPage.tsx
