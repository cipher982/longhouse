#!/usr/bin/env bash
set -euo pipefail

echo '## Update sauron README to remove Life Hub dependencies.'

echo '\n$ rg -n 'Life Hub' apps/sauron/README.md'
rg -n 'Life Hub' apps/sauron/README.md
