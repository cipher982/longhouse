#!/usr/bin/env bash
set -euo pipefail

echo '## Drop legacy React Flow selectors in CSS after test update.'

echo '\n$ sed -n '1,60p' apps/zerg/frontend-web/src/styles/canvas-react.css'
sed -n '1,60p' apps/zerg/frontend-web/src/styles/canvas-react.css
