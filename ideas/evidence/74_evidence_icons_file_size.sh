#!/usr/bin/env bash
set -euo pipefail

echo '## Split icons.tsx mega-file.'

echo '\n$ wc -l apps/zerg/frontend-web/src/components/icons.tsx'
wc -l apps/zerg/frontend-web/src/components/icons.tsx
