#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Load repo .env if present (local only; no auto-creation)
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT_DIR/.env"
  set +a
fi

FRONTEND_URL="${PLAYWRIGHT_BASE_URL:-${E2E_FRONTEND_URL:-https://swarmlet.com}}"
API_URL="${PLAYWRIGHT_API_BASE_URL:-${E2E_API_URL:-https://api.swarmlet.com}}"

if [[ -z "${SMOKE_TEST_SECRET:-}" ]]; then
  echo "SMOKE_TEST_SECRET is required for prod E2E (service-login)." >&2
  exit 1
fi

export PLAYWRIGHT_BASE_URL="$FRONTEND_URL"
export PLAYWRIGHT_API_BASE_URL="$API_URL"
export E2E_RUN_ID="${E2E_RUN_ID:-prod-$(date +%Y%m%d-%H%M%S)-$RANDOM}"

cd "$ROOT_DIR/apps/zerg/e2e"

bunx playwright test --config playwright.prod.config.js "$@"
