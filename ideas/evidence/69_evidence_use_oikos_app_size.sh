#!/usr/bin/env bash
set -euo pipefail

echo '## Split useOikosApp.ts (large file).'

echo '\n$ wc -l apps/zerg/frontend-web/src/oikos/app/hooks/useOikosApp.ts'
wc -l apps/zerg/frontend-web/src/oikos/app/hooks/useOikosApp.ts
