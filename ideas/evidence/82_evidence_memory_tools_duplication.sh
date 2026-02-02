#!/usr/bin/env bash
set -euo pipefail

echo '## Consolidate multiple memory tool modules into one API.'

echo '\n$ rg --files apps/zerg/backend/zerg/tools/builtin -g '*memory*tools.py''
rg --files apps/zerg/backend/zerg/tools/builtin -g '*memory*tools.py'
