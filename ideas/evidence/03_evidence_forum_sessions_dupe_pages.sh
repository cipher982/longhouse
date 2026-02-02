#!/usr/bin/env bash
set -euo pipefail

echo '## Consolidate ForumPage and SessionsPage into single Timeline experience.'

echo '\n$ rg -n 'ForumPage' apps/zerg/frontend-web/src/routes/App.tsx'
rg -n 'ForumPage' apps/zerg/frontend-web/src/routes/App.tsx
echo '\n$ rg -n 'SessionsPage' apps/zerg/frontend-web/src/routes/App.tsx'
rg -n 'SessionsPage' apps/zerg/frontend-web/src/routes/App.tsx
