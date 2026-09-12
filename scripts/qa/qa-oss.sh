#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
ROOT_DIR="$(cd "$ROOT_DIR" && pwd -P)"
if ! python3 "$(dirname "${BASH_SOURCE[0]}")/test_boundary.py"; then
  cat >&2 <<'EOF'
❌ OSS QA must run through the isolated dispatcher.
Use `make qa-oss`; direct host execution is refused.
EOF
  exit 2
fi


WORKDIR=""
RUN_UNIT=1
RUN_CORE_E2E=1
RUN_UI=1
PORT=""
ONBOARDING_PLAYWRIGHT_PROJECT="${ONBOARDING_PLAYWRIGHT_PROJECT:-}"

usage() {
  cat <<'USAGE'
Usage: scripts/qa-oss.sh [options]

Options:
  --workdir <path>   Use the prepared isolated workspace (default: repository root)
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

if [[ -z "$WORKDIR" ]]; then
  WORKDIR="$ROOT_DIR"
else
  if [[ ! -d "$WORKDIR" ]]; then
    echo "❌ Workdir does not exist: $WORKDIR" >&2
    exit 1
  fi
  WORKDIR="$(cd "$WORKDIR" && pwd -P)"
fi
if [[ "$WORKDIR" != "$ROOT_DIR" ]]; then
  echo "❌ Isolated OSS QA may only use its prepared repository workspace." >&2
  exit 1
fi
echo "📦 Using prepared isolated workspace at $WORKDIR"

require_prepared_workspace() {
  if [[ ! -x "$WORKDIR/server/.venv/bin/python" ]]; then
    echo "❌ Isolated workspace is missing the prepared server/.venv." >&2
    exit 1
  fi
  if [[ ! -d "$WORKDIR/node_modules" ]]; then
    echo "❌ Isolated workspace is missing the prepared JavaScript dependencies." >&2
    exit 1
  fi
  if [[ "$RUN_UI" -eq 1 && ! -x "$WORKDIR/node_modules/.bin/playwright" ]]; then
    echo "❌ Isolated workspace is missing the prepared Playwright CLI." >&2
    exit 1
  fi
}

require_prepared_workspace

QA_HOME="$WORKDIR/.qa-home"
SERVER_PID=""
SERVER_LOG="$WORKDIR/qa-oss-server.log"

cleanup() {
  if [[ -n "$SERVER_PID" ]]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi

}
trap cleanup EXIT

echo "🏗️  Building frontend dist from prepared dependencies..."
(cd "$WORKDIR/web" && bun run build)

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
QA_DB_PATH="${QA_HOME}/.longhouse/qa.db"
rm -rf "$QA_HOME"
mkdir -p "$(dirname "$QA_DB_PATH")"

echo "🚀 Starting Longhouse at ${BASE_URL}"
(
  cd "$WORKDIR/server"
  HOME="$QA_HOME" AUTH_DISABLED=1 ENVIRONMENT="test:e2e" LLM_DISABLED=1 SKIP_DEMO_SEED=1 DATABASE_URL="sqlite:///${QA_DB_PATH}" \
    uv run longhouse-server serve --host 127.0.0.1 --port "$PORT"
) >"$SERVER_LOG" 2>&1 &
SERVER_PID=$!

echo "⏳ Waiting for /api/health..."
ready=0
for _ in $(seq 1 60); do
  if curl -fsS "${BASE_URL}/api/health" >/dev/null 2>&1; then
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
  (
    cd "$WORKDIR/e2e"
    playwright_args=(test --config playwright.onboarding.config.js)
    if [[ -n "$ONBOARDING_PLAYWRIGHT_PROJECT" ]]; then
      playwright_args+=(--project "$ONBOARDING_PLAYWRIGHT_PROJECT")
    fi
    PLAYWRIGHT_BASE_URL="$BASE_URL" "$WORKDIR/node_modules/.bin/playwright" \
      "${playwright_args[@]}"
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
