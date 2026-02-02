#!/usr/bin/env bash
set -euo pipefail

echo '## Consolidate commis artifact and tool output stores into one subsystem.'

echo '\n$ rg --files apps/zerg/backend/zerg/services -g 'commis_*store.py' -g 'tool_output_store.py''
rg --files apps/zerg/backend/zerg/services -g 'commis_*store.py' -g 'tool_output_store.py'
