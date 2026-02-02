#!/usr/bin/env bash
set -euo pipefail

echo '## Move legacy Postgres test suite out of core repo.'

echo '\n$ sed -n '1,80p' apps/zerg/backend/tests/README.md'
sed -n '1,80p' apps/zerg/backend/tests/README.md
