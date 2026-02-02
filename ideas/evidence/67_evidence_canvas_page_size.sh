#!/usr/bin/env bash
set -euo pipefail

echo '## Split CanvasPage.tsx (large file).'

echo '\n$ wc -l apps/zerg/frontend-web/src/pages/CanvasPage.tsx'
wc -l apps/zerg/frontend-web/src/pages/CanvasPage.tsx
