#!/usr/bin/env bash
set -euo pipefail

echo '## Split SessionsPage.tsx into smaller components.'

echo '\n$ wc -l apps/zerg/frontend-web/src/pages/SessionsPage.tsx'
wc -l apps/zerg/frontend-web/src/pages/SessionsPage.tsx
