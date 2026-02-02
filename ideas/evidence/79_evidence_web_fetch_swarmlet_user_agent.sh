#!/usr/bin/env bash
set -euo pipefail

echo '## Update Swarmlet user-agent branding in web_fetch tool.'

echo '\n$ rg -n 'Swarmlet' apps/zerg/backend/zerg/tools/builtin/web_fetch.py'
rg -n 'Swarmlet' apps/zerg/backend/zerg/tools/builtin/web_fetch.py
