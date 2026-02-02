#!/usr/bin/env bash
set -euo pipefail

echo '## Remove legacy util margin helpers once migrated.'

echo '\n$ sed -n '1,40p' apps/zerg/frontend-web/src/styles/css/util.css'
sed -n '1,40p' apps/zerg/frontend-web/src/styles/css/util.css
