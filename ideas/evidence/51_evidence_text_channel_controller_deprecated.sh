#!/usr/bin/env bash
set -euo pipefail

echo '## Remove deprecated TextChannelController.'

echo '\n$ rg -n '@deprecated' apps/zerg/frontend-web/src/oikos/lib/text-channel-controller.ts'
rg -n '@deprecated' apps/zerg/frontend-web/src/oikos/lib/text-channel-controller.ts
