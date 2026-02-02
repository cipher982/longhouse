#!/usr/bin/env bash
set -euo pipefail

echo '## Replace postgresql.UUID in device tokens migration.'

echo '\n$ rg -n 'postgresql' apps/zerg/backend/alembic/versions/0004_device_tokens.py'
rg -n 'postgresql' apps/zerg/backend/alembic/versions/0004_device_tokens.py
