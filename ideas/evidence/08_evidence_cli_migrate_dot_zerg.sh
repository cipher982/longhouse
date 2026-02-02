#!/usr/bin/env bash
set -euo pipefail

echo '## Remove ~/.zerg migration path once longhouse is canonical.'

echo '\n$ rg -n '\\.zerg' apps/zerg/backend/zerg/cli/serve.py'
rg -n '\\.zerg' apps/zerg/backend/zerg/cli/serve.py
