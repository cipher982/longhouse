#!/usr/bin/env bash
set -euo pipefail

echo '## Remove run_backend_tests.sh legacy Postgres runner.'

echo '\n$ sed -n '1,80p' apps/zerg/backend/run_backend_tests.sh'
sed -n '1,80p' apps/zerg/backend/run_backend_tests.sh
