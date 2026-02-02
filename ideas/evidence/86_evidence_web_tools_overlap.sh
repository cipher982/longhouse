#!/usr/bin/env bash
set -euo pipefail

echo '## Consider merging web_search and web_fetch into a single web tool.'

echo '\n$ rg --files apps/zerg/backend/zerg/tools/builtin -g 'web_*''
rg --files apps/zerg/backend/zerg/tools/builtin -g 'web_*'
