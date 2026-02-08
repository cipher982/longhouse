#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# run-vibetest.sh — Launch vibetest browser agents against a Longhouse instance
#
# Modes:
#   Isolated (default): spawns its own server on a random port
#   Against running server: --use-running URL
#
# Advisory only — LLM-powered agents are non-deterministic. Never gate on this.
# ---------------------------------------------------------------------------

ROOT_DIR="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
VIBETEST_DIR="${VIBETEST_DIR:-$HOME/git/vibetest-use}"
VIBETEST_PYTHON="${VIBETEST_DIR}/.venv/bin/python"
RESULTS_DIR="${ROOT_DIR}/vibetest-results"

NUM_AGENTS="${VIBETEST_AGENTS:-3}"
HEADLESS=1
PORT=""
USE_RUNNING=""
SERVER_PID=""

usage() {
  cat <<'USAGE'
Usage: scripts/run-vibetest.sh [options]

Options:
  --use-running URL  Test against an already-running server (skip server spawn)
  --agents N         Number of browser agents (default: 3, env: VIBETEST_AGENTS)
  --headed           Show browser windows (default: headless)
  --port PORT        Fixed port for isolated server (default: random)
  -h, --help         Show this help

Examples:
  ./scripts/run-vibetest.sh                          # Isolated server, headless
  ./scripts/run-vibetest.sh --use-running http://localhost:47200
  ./scripts/run-vibetest.sh --agents 5 --headed
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    --use-running)
      USE_RUNNING="${2:-}"
      shift 2
      ;;
    --agents)
      NUM_AGENTS="${2:-3}"
      shift 2
      ;;
    --headed)
      HEADLESS=0
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
      echo "Unknown option: $1"
      usage
      exit 1
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

# GOOGLE_API_KEY required by vibetest (Gemini)
if [ -z "${GOOGLE_API_KEY:-}" ]; then
  echo "GOOGLE_API_KEY not set — skipping vibetest (needs Gemini API access)"
  exit 0
fi

# vibetest-use must be installed
if [ ! -x "$VIBETEST_PYTHON" ]; then
  echo "vibetest-use not found at $VIBETEST_DIR"
  echo ""
  echo "Install it:"
  echo "  cd ~/git && git clone https://github.com/cipher982/vibetest-use.git"
  echo "  cd vibetest-use && uv venv && source .venv/bin/activate"
  echo "  uv pip install -e . && playwright install chromium --with-deps --no-shell"
  echo ""
  echo "Or set VIBETEST_DIR to point to your checkout."
  exit 1
fi

# ---------------------------------------------------------------------------
# Cleanup trap
# ---------------------------------------------------------------------------
cleanup() {
  if [ -n "$SERVER_PID" ]; then
    kill "$SERVER_PID" >/dev/null 2>&1 || true
    wait "$SERVER_PID" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Determine target URL
# ---------------------------------------------------------------------------
if [ -n "$USE_RUNNING" ]; then
  BASE_URL="$USE_RUNNING"
  echo "Testing against running server: $BASE_URL"
else
  # Spawn isolated server
  if [ -z "$PORT" ]; then
    PORT="$(python3 -c 'import socket; s=socket.socket(); s.bind(("",0)); print(s.getsockname()[1]); s.close()')"
  fi
  BASE_URL="http://127.0.0.1:${PORT}"

  TMPDIR_VT="$(mktemp -d -t vibetest-srv-XXXXXX)"
  QA_HOME="${TMPDIR_VT}/.qa-home"
  DEMO_DB="${QA_HOME}/.longhouse/vibetest.db"
  SERVER_LOG="${TMPDIR_VT}/server.log"
  mkdir -p "$(dirname "$DEMO_DB")"

  echo "Starting isolated Longhouse at ${BASE_URL}..."
  (
    cd "${ROOT_DIR}/apps/zerg/backend"
    HOME="$QA_HOME" AUTH_DISABLED=1 ENVIRONMENT="test:vibetest" DATABASE_URL="sqlite:///${DEMO_DB}" \
      uv run longhouse serve --demo-fresh --host 127.0.0.1 --port "$PORT"
  ) >"$SERVER_LOG" 2>&1 &
  SERVER_PID=$!

  echo "Waiting for /api/health..."
  ready=0
  for _ in $(seq 1 60); do
    if curl -fsS "${BASE_URL}/api/health" >/dev/null 2>&1; then
      ready=1
      break
    fi
    sleep 1
  done

  if [ "$ready" -ne 1 ]; then
    echo "Server failed to become ready. Log tail:"
    tail -n 40 "$SERVER_LOG" || true
    exit 1
  fi
  echo "Server ready."
fi

# ---------------------------------------------------------------------------
# Run vibetest
# ---------------------------------------------------------------------------
mkdir -p "$RESULTS_DIR"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
RESULT_FILE="${RESULTS_DIR}/${TIMESTAMP}.json"

HEADLESS_FLAG="True"
if [ "$HEADLESS" -eq 0 ]; then
  HEADLESS_FLAG="False"
fi

echo "Running vibetest: ${NUM_AGENTS} agents, headless=${HEADLESS_FLAG}"
echo "  URL: ${BASE_URL}"
echo ""

"$VIBETEST_PYTHON" -c "
import asyncio, json, sys, os, time

# Suppress noisy logging from browser-use / langchain
import logging
logging.disable(logging.CRITICAL)
os.environ['ANONYMIZED_TELEMETRY'] = 'false'
os.environ['BROWSER_USE_LOGGING_LEVEL'] = 'CRITICAL'

sys.path.insert(0, '${VIBETEST_DIR}')
from vibetest.agents import run_pool, summarize_bug_reports

async def main():
    start = time.time()
    test_id = await run_pool(
        '${BASE_URL}',
        num_agents=${NUM_AGENTS},
        headless=${HEADLESS_FLAG},
    )
    summary = await summarize_bug_reports(test_id)
    summary['duration_seconds'] = round(time.time() - start, 1)
    return summary

summary = asyncio.run(main())

# Write JSON results
with open('${RESULT_FILE}', 'w') as f:
    json.dump(summary, f, indent=2, default=str)

# Print human-readable summary
status = summary.get('status_emoji', '?')
desc = summary.get('status_description', 'Unknown')
total = summary.get('total_issues', 0)
high = len(summary.get('severity_breakdown', {}).get('high_severity', []))
med = len(summary.get('severity_breakdown', {}).get('medium_severity', []))
low = len(summary.get('severity_breakdown', {}).get('low_severity', []))
dur = summary.get('duration_seconds', '?')
ok = summary.get('successful_agents', 0)
fail = summary.get('failed_agents', 0)

print()
print(f'{status} Vibetest complete — {desc}')
print(f'   Agents: {ok} succeeded, {fail} failed')
print(f'   Issues: {total} total (high={high}, medium={med}, low={low})')
print(f'   Duration: {dur}s')
print(f'   Results: ${RESULT_FILE}')

# Print high-severity details if any
if high > 0:
    print()
    print('High-severity issues:')
    for issue in summary.get('severity_breakdown', {}).get('high_severity', []):
        cat = issue.get('category', '?')
        desc_i = issue.get('description', '?')
        print(f'  [{cat}] {desc_i}')
"

EXIT_CODE=$?

# ---------------------------------------------------------------------------
# Stop isolated server
# ---------------------------------------------------------------------------
if [ -n "$SERVER_PID" ]; then
  echo ""
  echo "Stopping isolated server..."
  kill "$SERVER_PID" >/dev/null 2>&1 || true
  wait "$SERVER_PID" >/dev/null 2>&1 || true
  SERVER_PID=""
fi

exit $EXIT_CODE
