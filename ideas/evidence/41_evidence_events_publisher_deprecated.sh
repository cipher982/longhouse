#!/usr/bin/env bash
set -euo pipefail

echo '## Remove deprecated publish_event_safe wrapper.'

echo '\n$ rg -n 'deprecated' apps/zerg/backend/zerg/events/publisher.py'
rg -n 'deprecated' apps/zerg/backend/zerg/events/publisher.py
