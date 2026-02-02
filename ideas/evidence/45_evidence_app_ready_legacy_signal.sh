#!/usr/bin/env bash
set -euo pipefail

echo '## Remove __APP_READY__ legacy test signal once tests updated.'

echo '\n$ rg -n '__APP_READY__' apps/zerg/frontend-web/src/routes/App.tsx'
rg -n '__APP_READY__' apps/zerg/frontend-web/src/routes/App.tsx
