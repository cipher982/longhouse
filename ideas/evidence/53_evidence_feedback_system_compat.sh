#!/usr/bin/env bash
set -euo pipefail

echo '## Remove compatibility methods in feedback system.'

echo '\n$ rg -n 'compatibility' apps/zerg/frontend-web/src/oikos/lib/feedback-system.ts'
rg -n 'compatibility' apps/zerg/frontend-web/src/oikos/lib/feedback-system.ts
