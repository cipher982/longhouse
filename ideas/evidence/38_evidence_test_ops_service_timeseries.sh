#!/usr/bin/env bash
set -euo pipefail

echo '## Revisit timeseries compatibility tests tied to Postgres assumptions.'

echo '\n$ rg -n 'timeseries' apps/zerg/backend/tests/test_ops_service.py'
rg -n 'timeseries' apps/zerg/backend/tests/test_ops_service.py
