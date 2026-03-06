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
if [[ -f "$HOSTED_INSTANCE_HELPER" ]]; then
  # shellcheck disable=SC1090
  . "$HOSTED_INSTANCE_HELPER"
fi

CONTROL_PLANE_URL="${CONTROL_PLANE_URL:-${CP_URL:-https://control.longhouse.ai}}"
CP_URL="$CONTROL_PLANE_URL"
INSTANCE_SUBDOMAIN="${INSTANCE_SUBDOMAIN:-${E2E_INSTANCE_SUBDOMAIN:-}}"

if [[ -z "$INSTANCE_SUBDOMAIN" && -z "${PLAYWRIGHT_BASE_URL:-${E2E_FRONTEND_URL:-}}" && -n "${CONTROL_PLANE_ADMIN_TOKEN:-}" ]]; then
  INSTANCE_SUBDOMAIN="david010"
fi

FRONTEND_URL="${PLAYWRIGHT_BASE_URL:-${E2E_FRONTEND_URL:-${FRONTEND_URL:-}}}"
API_URL="${PLAYWRIGHT_API_BASE_URL:-${E2E_API_URL:-${API_URL:-$FRONTEND_URL}}}"

if [[ -n "$INSTANCE_SUBDOMAIN" ]]; then
  if [[ ! -f "$HOSTED_INSTANCE_HELPER" ]]; then
    echo "Hosted instance helper missing: $HOSTED_INSTANCE_HELPER" >&2
    exit 1
  fi

  lh_hosted_resolve_instance "$INSTANCE_SUBDOMAIN"
  FRONTEND_URL="${PLAYWRIGHT_BASE_URL:-${E2E_FRONTEND_URL:-${FRONTEND_URL:-$LH_INSTANCE_URL}}}"
  API_URL="${PLAYWRIGHT_API_BASE_URL:-${E2E_API_URL:-${API_URL:-$LH_INSTANCE_URL}}}"
  SMOKE_LOGIN_TOKEN="${SMOKE_LOGIN_TOKEN:-$(lh_hosted_issue_login_token "$LH_INSTANCE_ID")}"
fi

if [[ -z "$FRONTEND_URL" || -z "$API_URL" ]]; then
  echo "Set INSTANCE_SUBDOMAIN + CONTROL_PLANE_* or PLAYWRIGHT_BASE_URL/PLAYWRIGHT_API_BASE_URL before running prod E2E." >&2
  exit 1
fi

if [[ -z "${SMOKE_LOGIN_TOKEN:-}" ]]; then
  echo "Set SMOKE_LOGIN_TOKEN or INSTANCE_SUBDOMAIN + CONTROL_PLANE_* before running prod E2E." >&2
  exit 1
fi

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
