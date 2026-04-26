#!/usr/bin/env bash
# Run the Playwright render-canary check against a hosted instance.
#
# Measures real browser EventSource arrival → DOM paint latency using a
# patched EventSource + rAF. Fails deploy if p95 exceeds SLA.
#
# Env (required):
#   LONGHOUSE_CANARY_SESSION_ID
#   LONGHOUSE_CANARY_TOKEN
# Env (optional):
#   QA_INSTANCE_SUBDOMAIN     (default: david010)
#   RENDER_CANARY_SLA_P95_MS  (default: 500)
#   RENDER_CANARY_WINDOW_MS   (default: 90000)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER="$ROOT_DIR/scripts/qa/run-prod-e2e.sh"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT_DIR/.env"
  set +a
fi

INSTANCE_SUBDOMAIN="${QA_INSTANCE_SUBDOMAIN:-${INSTANCE_SUBDOMAIN:-david010}}"
INSTANCE_URL="${QA_INSTANCE_URL:-${PLAYWRIGHT_BASE_URL:-}}"

if [[ -n "$INSTANCE_URL" ]]; then
  INSTANCE_URL="${INSTANCE_URL%/}"
  export PLAYWRIGHT_BASE_URL="$INSTANCE_URL"
  export PLAYWRIGHT_API_BASE_URL="${PLAYWRIGHT_API_BASE_URL:-$INSTANCE_URL}"
else
  export INSTANCE_SUBDOMAIN="$INSTANCE_SUBDOMAIN"
fi

if [[ -z "${LONGHOUSE_CANARY_SESSION_ID:-}" ]]; then
  echo "LONGHOUSE_CANARY_SESSION_ID not set; canary producer must have bootstrapped a session first." >&2
  exit 2
fi

if [[ -z "${LONGHOUSE_CANARY_TOKEN:-}" ]]; then
  echo "LONGHOUSE_CANARY_TOKEN not set; required to post hop=render observations." >&2
  exit 2
fi

export LONGHOUSE_CANARY_SESSION_ID
export LONGHOUSE_CANARY_TOKEN

exec "$RUNNER" tests/live/render-canary.spec.ts --timeout=180000 --reporter=line "$@"
