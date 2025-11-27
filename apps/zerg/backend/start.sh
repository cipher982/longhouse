#!/bin/bash
set -e

# Ensure we're in the right directory for alembic
cd /app

# Master runtime validation - fail fast if environment is broken
echo "🔍 Running master runtime validation..."
python validate-runtime.py || {
    echo "❌ CRITICAL: Runtime validation failed!"
    echo "❌ Container startup blocked - check environment configuration"
    exit 1
}

# Create migration log file accessible via web
MIGRATION_LOG="/app/static/migration.log"
mkdir -p /app/static

echo "=== Migration Log $(date) ===" > "$MIGRATION_LOG"

# Output to both console and log file
{
    echo "🔄 Running database migrations..."
    echo "Working directory: $(pwd)"
    echo "Alembic config exists: $(test -f alembic.ini && echo 'YES' || echo 'NO')"
    echo "Python path: $(which python)"
    echo "Alembic module check:"
    python -c 'import alembic; print(f"Alembic version: {alembic.__version__}")' 2>&1 || echo 'ALEMBIC NOT FOUND'
    echo "✅ Runtime validation completed"
    
    echo "Running alembic upgrade head..."
    python -m alembic upgrade head 2>&1 || {
        echo "❌ Migration failed with exit code $?"
        echo "Migration FAILED - server starting anyway"
    }
    
    echo "✅ Migration process complete"
} 2>&1 | tee "$MIGRATION_LOG"

# Also output to stderr so it appears in Docker logs
cat "$MIGRATION_LOG" >&2

echo "Starting server..."
exec python -m uvicorn zerg.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'