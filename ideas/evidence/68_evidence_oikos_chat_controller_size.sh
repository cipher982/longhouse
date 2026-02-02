#!/usr/bin/env bash
set -euo pipefail

echo '## Split oikos-chat-controller.ts (large file).'

echo '\n$ wc -l apps/zerg/frontend-web/src/oikos/lib/oikos-chat-controller.ts'
wc -l apps/zerg/frontend-web/src/oikos/lib/oikos-chat-controller.ts
