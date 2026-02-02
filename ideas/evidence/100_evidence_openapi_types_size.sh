#!/usr/bin/env bash
set -euo pipefail

echo '## Split generated openapi-types.ts to reduce bundle weight.'

echo '\n$ wc -l apps/zerg/frontend-web/src/generated/openapi-types.ts'
wc -l apps/zerg/frontend-web/src/generated/openapi-types.ts
