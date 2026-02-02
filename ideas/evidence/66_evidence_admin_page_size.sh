#!/usr/bin/env bash
set -euo pipefail

echo '## Split AdminPage.tsx (large file).'

echo '\n$ wc -l apps/zerg/frontend-web/src/pages/AdminPage.tsx'
wc -l apps/zerg/frontend-web/src/pages/AdminPage.tsx
