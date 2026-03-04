#!/usr/bin/env bash
# Self-contained marketing screenshot capture.
# Starts demo environment, captures screenshots, then exits.
# No pre-running stack needed.
#
# Usage:
#   ./scripts/marketing-screenshots.sh           # Capture all
#   ./scripts/marketing-screenshots.sh chat-preview  # Capture one by name
set -e

cd "$(dirname "$0")/.."

DEMO_DB_PATH="${DEMO_DB_PATH:-$PWD/data/demo/longhouse-demo.db}"
BACKEND_PORT=47399
FRONTEND_PORT=47398
BASE_URL="http://localhost:$FRONTEND_PORT"
BACKEND_PID=""
FRONTEND_PID=""

cleanup() {
    echo ""
    echo "Stopping dev stack..."
    [ -n "$FRONTEND_PID" ] && kill "$FRONTEND_PID" 2>/dev/null || true
    [ -n "$BACKEND_PID" ] && kill "$BACKEND_PID" 2>/dev/null || true
    # Kill any children (uvicorn workers, vite)
    pkill -P "$BACKEND_PID" 2>/dev/null || true
    pkill -P "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT

# --- Env ---
unset DATABASE_URL
export DATABASE_URL="sqlite:///$DEMO_DB_PATH"
export AUTH_DISABLED=1
export SINGLE_TENANT=1
export VITE_PROXY_TARGET="http://localhost:$BACKEND_PORT"

if [ -z "$FERNET_SECRET" ]; then
    FERNET_KEY_FILE="$HOME/.longhouse/fernet.key"
    mkdir -p "$HOME/.longhouse"
    if [ -f "$FERNET_KEY_FILE" ]; then
        export FERNET_SECRET=$(cat "$FERNET_KEY_FILE")
    else
        export FERNET_SECRET=$(python3 -c "import base64, secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())")
        echo "$FERNET_SECRET" > "$FERNET_KEY_FILE"
        chmod 600 "$FERNET_KEY_FILE"
    fi
fi

# --- Build demo DB ---
echo "Building demo database..."
rm -f "$DEMO_DB_PATH"
mkdir -p "$(dirname "$DEMO_DB_PATH")"
(cd apps/zerg/backend && uv run python scripts/build_demo_db.py --output "$DEMO_DB_PATH")

# --- Start backend ---
echo "Starting backend on :$BACKEND_PORT..."
(cd apps/zerg/backend && uv run uvicorn zerg.main:app --host 0.0.0.0 --port "$BACKEND_PORT") &
BACKEND_PID=$!

for i in {1..30}; do
    if curl -sf "http://localhost:$BACKEND_PORT/api/health" >/dev/null 2>&1; then
        echo "Backend ready"
        break
    fi
    [ "$i" -eq 30 ] && { echo "Backend failed to start"; exit 1; }
    sleep 1
done

# --- Start frontend (Vite dev) ---
echo "Starting frontend on :$FRONTEND_PORT..."
(cd apps/zerg/frontend-web && bun run dev --port "$FRONTEND_PORT" --logLevel warn) &
FRONTEND_PID=$!

for i in {1..30}; do
    if curl -sf "$BASE_URL" >/dev/null 2>&1; then
        echo "Frontend ready"
        break
    fi
    [ "$i" -eq 30 ] && { echo "Frontend failed to start"; exit 1; }
    sleep 1
done

# --- Capture ---
echo ""
if [ -n "$1" ]; then
    uv run --with playwright --with pyyaml scripts/capture_marketing.py \
        --base-url "$BASE_URL" --name "$1"
else
    uv run --with playwright --with pyyaml scripts/capture_marketing.py \
        --base-url "$BASE_URL"
fi
