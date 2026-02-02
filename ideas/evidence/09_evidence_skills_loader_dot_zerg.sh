#!/usr/bin/env bash
set -euo pipefail

echo '## Update skills loader to use ~/.longhouse only (no ~/.zerg fallback).'

echo '\n$ rg -n '\\.zerg' apps/zerg/backend/zerg/skills/loader.py'
rg -n '\\.zerg' apps/zerg/backend/zerg/skills/loader.py
