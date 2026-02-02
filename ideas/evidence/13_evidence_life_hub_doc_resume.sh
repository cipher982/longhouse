#!/usr/bin/env bash
set -euo pipefail

echo '## Update VISION session resume section to remove Life Hub flow.'

echo '\n$ rg -n "Life Hub" VISION.md'
rg -n 'Life Hub' VISION.md
