#!/bin/bash
set -e

# Development startup script - runs migrations then uvicorn with hot reload
# This mirrors production behavior (start.sh) but with --reload for dev

cd /app

echo "=== Development Startup $(date) ==="

# Run database migrations
echo "Running database migrations..."
python -m alembic upgrade head 2>&1 || {
    echo "Migration failed with exit code $?"
    echo "Attempting to continue anyway..."
}
echo "Migrations complete"

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
