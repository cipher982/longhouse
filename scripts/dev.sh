#!/usr/bin/env bash
# Native development environment (SQLite, no Docker)
set -e

cd "$(dirname "$0")/.."

echo "📦 Setting up SQLite development environment..."

# Force SQLite - ignore any DATABASE_URL from .env
unset DATABASE_URL
export DATABASE_URL="sqlite:///$HOME/.longhouse/dev.db"
export AUTH_DISABLED="${AUTH_DISABLED:-1}"
export SINGLE_TENANT="${SINGLE_TENANT:-1}"

# Generate Fernet key if not set
if [ -z "$FERNET_SECRET" ]; then
    FERNET_KEY_FILE="$HOME/.longhouse/fernet.key"
    mkdir -p "$HOME/.longhouse"
    if [ -f "$FERNET_KEY_FILE" ]; then
        export FERNET_SECRET=$(cat "$FERNET_KEY_FILE")
    else
        export FERNET_SECRET=$(python3 -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())")
        echo "$FERNET_SECRET" > "$FERNET_KEY_FILE"
        chmod 600 "$FERNET_KEY_FILE"
        echo "  Generated FERNET_SECRET in $FERNET_KEY_FILE"
    fi
fi

echo "  DATABASE_URL: $DATABASE_URL"
echo "  AUTH_DISABLED: $AUTH_DISABLED"
echo ""

# Check dependencies
if ! command -v uv &> /dev/null; then
    echo "❌ uv not found. Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

if ! command -v bun &> /dev/null; then
    echo "❌ bun not found. Install: curl -fsSL https://bun.sh/install | bash"
    exit 1
fi

# Install backend deps if needed
if [ ! -d "apps/zerg/backend/.venv" ]; then
    echo "📦 Installing backend dependencies..."
    (cd apps/zerg/backend && uv sync)
fi

# Install frontend deps if needed
if [ ! -d "node_modules" ]; then
    echo "📦 Installing frontend dependencies..."
    bun install
fi

# Function to cleanup on exit
cleanup() {
    echo ""
    echo "🛑 Shutting down..."
    kill $BACKEND_PID 2>/dev/null || true
    kill $FRONTEND_PID 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

# Start backend
echo "🚀 Starting backend (port 47300)..."
(cd apps/zerg/backend && uv run uvicorn zerg.main:app --host 0.0.0.0 --port 47300 --reload) &
BACKEND_PID=$!

# Wait for backend to be ready
echo "  Waiting for backend..."
for i in {1..30}; do
    if curl -s http://localhost:47300/api/health > /dev/null 2>&1; then
        echo "  ✅ Backend ready"
        break
    fi
    sleep 1
done

# Start frontend
echo "🚀 Starting frontend (port 47200)..."
(cd apps/zerg/frontend-web && bun run dev --port 47200) &
FRONTEND_PID=$!

echo ""
echo "✅ Development environment ready"
echo ""
echo "  Backend:  http://localhost:47300"
echo "  Frontend: http://localhost:47200"
echo "  Database: $DATABASE_URL"
echo ""
echo "Press Ctrl+C to stop"

# Wait for processes
wait
