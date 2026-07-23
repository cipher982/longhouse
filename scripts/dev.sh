#!/usr/bin/env bash
# Local frontend development against the Runtime Host already linked to this machine.
set -e

cd "$(dirname "$0")/.."

TARGET_FILE="$HOME/.longhouse/machine/target-url"
TOKEN_FILE="$HOME/.longhouse/machine/device-token"

if [ ! -s "$TARGET_FILE" ] || [ ! -s "$TOKEN_FILE" ]; then
    echo "Longhouse is not linked to a Runtime Host."
    echo "Run 'longhouse auth --url https://your-runtime.example', then retry 'make dev'."
    echo "Use 'make dev-demo' for an isolated local demo runtime."
    exit 1
fi

TARGET_URL=$(tr -d '\r\n' < "$TARGET_FILE")

if ! command -v bun &> /dev/null; then
    echo "bun not found. Install: curl -fsSL https://bun.sh/install | bash"
    exit 1
fi

if [ ! -d "node_modules" ]; then
    bun install
fi

# The Vite dev proxy reads the linked target and device token directly. The repo
# .env still contains the old local-backend target, so it must not override the
# authenticated Runtime Host for the normal development path.
unset VITE_PROXY_TARGET

cleanup() {
    echo ""
    echo "Shutting down local UI..."
    kill "$FRONTEND_PID" 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

echo "Starting local Longhouse UI..."
(cd web && bun run dev --port 47200) &
FRONTEND_PID=$!

echo ""
echo "Local UI: http://localhost:47200/timeline"
echo "Account:  $TARGET_URL"
echo ""
echo "Press Ctrl+C to stop"

wait
