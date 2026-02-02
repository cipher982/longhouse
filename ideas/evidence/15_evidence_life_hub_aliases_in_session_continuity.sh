#!/usr/bin/env bash
set -euo pipefail

echo '## Remove fetch or ship life hub aliases in session continuity.'

echo '\n$ rg -n 'life_hub' apps/zerg/backend/zerg/services/session_continuity.py'
rg -n 'life_hub' apps/zerg/backend/zerg/services/session_continuity.py
