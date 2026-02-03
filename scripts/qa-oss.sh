#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
WORKDIR=""
KEEP_WORKDIR=0
RUN_UNIT=1
RUN_CORE_E2E=1
RUN_UI=1
PORT=""

usage() {
  cat <<'USAGE'
Usage: scripts/qa-oss.sh [options]

Options:
  --workdir <path>   Use existing workspace (skip clone)
  --keep             Keep workspace after run
  --quick            Skip unit tests + core E2E (UI check only)
  --no-e2e           Skip core E2E suite
  --no-unit          Skip unit/onboarding tests
  --no-ui            Skip Playwright onboarding UI check
  --port <port>      Fixed port for local server (default: random free port)
USAGE
}

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "❌ Missing required command: $1"
    exit 1
  fi
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workdir)
      WORKDIR="${2:-}"
      shift 2
      ;;
    --keep)
      KEEP_WORKDIR=1
      shift
      ;;
    --quick)
      RUN_UNIT=0
      RUN_CORE_E2E=0
      shift
      ;;
    --no-e2e)
      RUN_CORE_E2E=0
      shift
      ;;
    --no-unit)
      RUN_UNIT=0
      shift
      ;;
    --no-ui)
      RUN_UI=0
      shift
      ;;
    --port)
      PORT="${2:-}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "❌ Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

require_cmd git
require_cmd uv
require_cmd bun
require_cmd curl
require_cmd python3

CLONED=0
if [[ -z "$WORKDIR" ]]; then
  WORKDIR="$(mktemp -d -t longhouse-oss-qa-XXXXXX)"
  CLONED=1
  echo "📦 Cloning repo into $WORKDIR"
  git clone --quiet "$ROOT_DIR" "$WORKDIR"
else
  if [[ ! -d "$WORKDIR" ]]; then
    echo "❌ Workdir does not exist: $WORKDIR"
    exit 1
  fi
  echo "📦 Using existing workspace at $WORKDIR"
fi

QA_HOME="$WORKDIR/.qa-home"
SERVER_PID=""
SERVER_LOG="$WORKDIR/qa-oss-server.log"

cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi

  if [[ "$KEEP_WORKDIR" -eq 0 && "$CLONED" -eq 1 ]]; then
    rm -rf "$WORKDIR"
  fi
}
trap cleanup EXIT

echo "🏗️  Building frontend dist..."
(cd "$WORKDIR/apps/zerg/frontend-web" && bun install --silent && bun run build)

if [[ "$RUN_UNIT" -eq 1 ]]; then
  echo "🧪 Running unit + onboarding-sqlite tests..."
  (cd "$WORKDIR" && make test)
  (cd "$WORKDIR" && make test-frontend-unit)
  (cd "$WORKDIR" && make onboarding-sqlite)
fi

if [[ -z "$PORT" ]]; then
  PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("", 0))
print(s.getsockname()[1])
s.close()
PY
)"
fi

BASE_URL="http://127.0.0.1:${PORT}"
DEMO_DB_PATH="${QA_HOME}/.longhouse/demo.db"
mkdir -p "$(dirname "$DEMO_DB_PATH")"

echo "🚀 Starting Longhouse (demo) at ${BASE_URL}"
(
  cd "$WORKDIR/apps/zerg/backend"
  HOME="$QA_HOME" AUTH_DISABLED=1 ENVIRONMENT="test:e2e" DATABASE_URL="sqlite:///${DEMO_DB_PATH}" \
    uv run longhouse serve --demo-fresh --host 127.0.0.1 --port "$PORT"
) >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

echo "⏳ Waiting for /health..."
ready=0
for _ in $(seq 1 60); do
  if curl -fsS "${BASE_URL}/health" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
done

if [[ "$ready" -ne 1 ]]; then
  echo "❌ Server failed to become ready. Log tail:"
  tail -n 120 "$SERVER_LOG" || true
  exit 1
fi

if [[ "$RUN_UI" -eq 1 ]]; then
  echo "🎭 Running onboarding UI check..."
  (
    cd "$WORKDIR/apps/zerg/e2e"
    bun install --silent
    PLAYWRIGHT_BASE_URL="$BASE_URL" bunx playwright test --config playwright.onboarding.config.js
  )
fi

echo "🛑 Stopping server..."
kill "$SERVER_PID" >/dev/null 2>&1 || true
wait "$SERVER_PID" >/dev/null 2>&1 || true
SERVER_PID=""

if [[ "$RUN_CORE_E2E" -eq 1 ]]; then
  echo "🎯 Running core E2E suite..."
  (cd "$WORKDIR" && make test-e2e-core)
fi

echo "✅ OSS QA complete."
