#!/usr/bin/env bash
set -euo pipefail

echo '## Split commis-progress-store.ts (large file).'

echo '\n$ wc -l apps/zerg/frontend-web/src/oikos/lib/commis-progress-store.ts'
wc -l apps/zerg/frontend-web/src/oikos/lib/commis-progress-store.ts
