#!/bin/bash
set -e

# Development startup script - runs migrations then uvicorn with hot reload
# This mirrors production behavior (start.sh) but with --reload for dev

cd /app

echo "=== Development Startup $(date) ==="

# Bootstrap fresh DBs: if no tables exist, create via SQLAlchemy and stamp head.
echo "Checking database state..."
TABLE_COUNT=$(python - <<'PY'
import os
from sqlalchemy import create_engine, inspect
from zerg.database import DB_SCHEMA

url = os.getenv("DATABASE_URL")
engine = create_engine(url)
inspector = inspect(engine)
if engine.dialect.name == "postgresql":
    tables = inspector.get_table_names(schema=DB_SCHEMA)
else:
    tables = inspector.get_table_names()
print(len(tables))
PY
)

if [ "$TABLE_COUNT" -eq 0 ]; then
    echo "No tables found; bootstrapping schema via SQLAlchemy..."
    python - <<'PY'
from zerg.database import initialize_database
initialize_database()
PY
    echo "Stamping alembic head..."
    python -m alembic stamp head 2>&1 || {
        echo "Alembic stamp failed with exit code $?"
    }
else
    echo "Running database migrations..."
    python -m alembic upgrade head 2>&1 || {
        echo "Migration failed with exit code $?"
        echo "Attempting to continue anyway..."
    }
    echo "Migrations complete"
fi

# Auto-seed user context if local config exists (idempotent)
echo "Checking for user context seed..."
python scripts/seed_user_context.py 2>&1 || echo "Context seeding skipped or failed (non-fatal)"

# Auto-seed personal tool credentials if local config exists (idempotent)
echo "Checking for personal credentials seed..."
python scripts/seed_personal_credentials.py 2>&1 || echo "Credentials seeding skipped or failed (non-fatal)"

echo "Starting uvicorn with hot reload..."
exec uvicorn zerg.main:app \
    --host 0.0.0.0 \
    --port 8000 \
    --reload \
    --reload-dir /app/zerg \
    --proxy-headers \
    --forwarded-allow-ips '*'
