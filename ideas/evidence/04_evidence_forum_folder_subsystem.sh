#!/usr/bin/env bash
set -euo pipefail

echo '## Collapse standalone forum subsystem into timeline; forum folder still exists.'

echo '\n$ ls apps/zerg/frontend-web/src/forum'
ls apps/zerg/frontend-web/src/forum
echo '\n$ rg -n 'path: "/forum"' apps/zerg/frontend-web/src/routes/App.tsx'
rg -n 'path: "/forum"' apps/zerg/frontend-web/src/routes/App.tsx
