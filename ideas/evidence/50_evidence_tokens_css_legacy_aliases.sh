#!/usr/bin/env bash
set -euo pipefail

echo '## Remove legacy token aliases after CSS migration.'

echo '\n$ rg -n 'Legacy' apps/zerg/frontend-web/src/styles/tokens.css'
rg -n 'Legacy' apps/zerg/frontend-web/src/styles/tokens.css
