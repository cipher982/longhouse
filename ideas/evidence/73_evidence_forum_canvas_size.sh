#!/usr/bin/env bash
set -euo pipefail

echo '## Split ForumCanvas.tsx (large file).'

echo '\n$ wc -l apps/zerg/frontend-web/src/forum/ForumCanvas.tsx'
wc -l apps/zerg/frontend-web/src/forum/ForumCanvas.tsx
