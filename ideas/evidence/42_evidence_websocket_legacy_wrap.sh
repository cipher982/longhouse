#!/usr/bin/env bash
set -euo pipefail

echo '## Require envelope-only WS messages, remove legacy wrapping.'

echo '\n$ rg -n 'legacy' apps/zerg/backend/zerg/websocket/handlers.py'
rg -n 'legacy' apps/zerg/backend/zerg/websocket/handlers.py
