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

echo "Checking migration state..."
eval "$(uv run python - <<'PY'
from zerg.config import get_settings
from zerg.database import make_engine
from sqlalchemy import text

settings = get_settings()
engine = make_engine(settings.database_url)

with engine.begin() as conn:
    has_alembic = conn.execute(
        text(
            "select 1 from information_schema.tables "
            "where table_schema='zerg' and table_name='alembic_version'"
        )
    ).fetchone() is not None

    has_agents_sessions = conn.execute(
        text(
            "select 1 from information_schema.tables "
            "where table_schema='agents' and table_name='sessions'"
        )
    ).fetchone() is not None

    has_provider_session_id = conn.execute(
        text(
            "select 1 from information_schema.columns "
            "where table_schema='agents' and table_name='sessions' "
            "and column_name='provider_session_id'"
        )
    ).fetchone() is not None

    has_raw_json = conn.execute(
        text(
            "select 1 from information_schema.columns "
            "where table_schema='agents' and table_name='events' "
            "and column_name='raw_json'"
        )
    ).fetchone() is not None

    has_device_tokens = conn.execute(
        text(
            "select 1 from information_schema.tables "
            "where table_schema='zerg' and table_name='device_tokens'"
        )
    ).fetchone() is not None

print(f"ALI_HAS_VERSION={int(has_alembic)}")
print(f"ALI_HAS_AGENTS={int(has_agents_sessions)}")
print(f"ALI_HAS_PROVIDER_SESSION_ID={int(has_provider_session_id)}")
print(f"ALI_HAS_RAW_JSON={int(has_raw_json)}")
print(f"ALI_HAS_DEVICE_TOKENS={int(has_device_tokens)}")
PY
)"

if [ "${ALI_HAS_VERSION}" -eq 0 ] && [ "${ALI_HAS_AGENTS}" -eq 1 ]; then
  if [ "${ALI_HAS_PROVIDER_SESSION_ID}" -eq 1 ] && [ "${ALI_HAS_RAW_JSON}" -eq 1 ]; then
    STAMP_TARGET="0003_raw_json_provider_session_id"
  else
    STAMP_TARGET="0002_agents_schema"
  fi
  echo "Alembic version missing; stamping to ${STAMP_TARGET}..."
  uv run alembic stamp "${STAMP_TARGET}"
fi

if [ "${ALI_HAS_DEVICE_TOKENS}" -eq 0 ]; then
  echo "Running migrations..."
  uv run alembic upgrade head
else
  echo "device_tokens table already present; skipping upgrade."
fi

echo "Verifying device_tokens table..."
uv run python - <<'PY'
from sqlalchemy import inspect
from zerg.config import get_settings
from zerg.database import make_engine

engine = make_engine(get_settings().database_url)
inspector = inspect(engine)
if "device_tokens" not in inspector.get_table_names(schema="zerg"):
    raise SystemExit("device_tokens table missing!")
print("device_tokens table exists")
PY

echo "=== Prerequisites complete ==="
