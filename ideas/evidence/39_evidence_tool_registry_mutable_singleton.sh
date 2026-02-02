#!/usr/bin/env bash
set -euo pipefail

echo '## Remove mutable ToolRegistry singleton once tests updated.'

echo '\n$ sed -n '1,140p' apps/zerg/backend/zerg/tools/registry.py'
sed -n '1,140p' apps/zerg/backend/zerg/tools/registry.py
