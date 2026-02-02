#!/usr/bin/env bash
set -euo pipefail

echo '## Remove TODO cron parsing in oikos_fiches router by moving to scheduler module.'

echo '\n$ rg -n 'TODO' apps/zerg/backend/zerg/routers/oikos_fiches.py'
rg -n 'TODO' apps/zerg/backend/zerg/routers/oikos_fiches.py
