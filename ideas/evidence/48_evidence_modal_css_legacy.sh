#!/usr/bin/env bash
set -euo pipefail

echo '## Remove legacy modal pattern CSS.'

echo '\n$ sed -n '1,40p' apps/zerg/frontend-web/src/styles/css/modal.css'
sed -n '1,40p' apps/zerg/frontend-web/src/styles/css/modal.css
