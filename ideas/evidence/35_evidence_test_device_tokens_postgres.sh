#!/usr/bin/env bash
set -euo pipefail

echo '## Remove device-token tests that expect Postgres-only behavior.'

echo '\n$ rg -n 'postgres' apps/zerg/backend/tests/test_device_tokens.py'
rg -n 'postgres' apps/zerg/backend/tests/test_device_tokens.py
