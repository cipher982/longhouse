#!/usr/bin/env bash
set -euo pipefail

echo '## Remove legacy buttons.css compatibility layer.'

echo '\n$ sed -n '1,40p' apps/zerg/frontend-web/src/styles/css/buttons.css'
sed -n '1,40p' apps/zerg/frontend-web/src/styles/css/buttons.css
