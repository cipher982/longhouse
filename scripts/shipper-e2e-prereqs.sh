#!/bin/bash
set -euo pipefail

BACKEND_DIR="$(cd "$(dirname "$0")/../apps/zerg/backend" && pwd)"

cd "$BACKEND_DIR"

echo "=== Shipper E2E Prerequisites ==="

echo "Checking for device_tokens migration..."
if ! ls alembic/versions/*device_tokens* >/dev/null 2>&1; then
  echo "ERROR: device_tokens migration missing. Did you pull latest?"
  exit 1
fi

echo "Running migrations..."
uv run alembic upgrade head

echo "Verifying device_tokens table..."
uv run python - <<'PY'
from sqlalchemy import inspect
from zerg.database import engine

inspector = inspect(engine)
if "device_tokens" not in inspector.get_table_names():
    raise SystemExit("device_tokens table missing!")
print("device_tokens table exists")
PY

echo "=== Prerequisites complete ==="
