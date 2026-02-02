#!/usr/bin/env bash
set -euo pipefail

echo '## Remove deprecated session handler API.'

echo '\n$ rg -n '@deprecated' apps/zerg/frontend-web/src/oikos/lib/session-handler.ts'
rg -n '@deprecated' apps/zerg/frontend-web/src/oikos/lib/session-handler.ts
