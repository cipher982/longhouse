#!/usr/bin/env bash
set -euo pipefail

echo '## Update session resume doc to remove Life Hub flow.'

echo '\n$ rg -n 'Life Hub' docs/session-resume-design.md'
rg -n 'Life Hub' docs/session-resume-design.md
