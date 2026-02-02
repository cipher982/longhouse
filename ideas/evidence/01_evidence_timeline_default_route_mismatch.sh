#!/usr/bin/env bash
set -euo pipefail

echo '## Make /timeline the default authenticated landing per VISION (root route currently landing or dashboard logic).'

echo '\n$ rg -n 'Timeline' VISION.md'
rg -n 'Timeline' VISION.md
echo '\n$ rg -n 'path: "/"' apps/zerg/frontend-web/src/routes/App.tsx'
rg -n 'path: "/"' apps/zerg/frontend-web/src/routes/App.tsx
echo '\n$ rg -n '/timeline' apps/zerg/frontend-web/src/routes/App.tsx'
rg -n '/timeline' apps/zerg/frontend-web/src/routes/App.tsx
