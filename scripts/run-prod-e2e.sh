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

HOSTED_INSTANCE_HELPER="$ROOT_DIR/scripts/lib/hosted-instance.sh"
if [[ ! -f "$HOSTED_INSTANCE_HELPER" ]]; then
  echo "Hosted instance helper missing: $HOSTED_INSTANCE_HELPER" >&2
  exit 1
fi

# shellcheck disable=SC1090
. "$HOSTED_INSTANCE_HELPER"

INSTANCE_SUBDOMAIN="${INSTANCE_SUBDOMAIN:-${E2E_INSTANCE_SUBDOMAIN:-}}"
FRONTEND_URL="${PLAYWRIGHT_BASE_URL:-${E2E_FRONTEND_URL:-${FRONTEND_URL:-}}}"
API_URL="${PLAYWRIGHT_API_BASE_URL:-${E2E_API_URL:-${API_URL:-$FRONTEND_URL}}}"

lh_hosted_prepare_target "$INSTANCE_SUBDOMAIN" "$FRONTEND_URL" "$API_URL" "david010"
FRONTEND_URL="$LH_TARGET_FRONTEND_URL"
API_URL="$LH_TARGET_API_URL"
INSTANCE_SUBDOMAIN="${LH_TARGET_SUBDOMAIN:-$INSTANCE_SUBDOMAIN}"
SMOKE_LOGIN_TOKEN="${SMOKE_LOGIN_TOKEN:-$(lh_hosted_resolved_login_token "$INSTANCE_SUBDOMAIN")}"

export PLAYWRIGHT_BASE_URL="$FRONTEND_URL"
export PLAYWRIGHT_API_BASE_URL="$API_URL"
export PLAYWRIGHT_FRONTEND_BASE="$FRONTEND_URL"
export PLAYWRIGHT_BACKEND_URL="$API_URL"
export FRONTEND_URL="$FRONTEND_URL"
export API_URL="$API_URL"
export RUN_LIVE_E2E="1"
export SMOKE_LOGIN_TOKEN
export E2E_RUN_ID="${E2E_RUN_ID:-prod-$(date +%Y%m%d-%H%M%S)-$RANDOM}"

cd "$ROOT_DIR/apps/zerg/e2e"

bunx playwright test --config playwright.prod.config.js "$@"
