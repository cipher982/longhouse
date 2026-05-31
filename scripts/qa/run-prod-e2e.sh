#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

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

lh_hosted_prepare_target "$INSTANCE_SUBDOMAIN" "$FRONTEND_URL" "$API_URL" "${LONGHOUSE_DEFAULT_SUBDOMAIN:-demo}"
FRONTEND_URL="$LH_TARGET_FRONTEND_URL"
API_URL="$LH_TARGET_API_URL"
INSTANCE_SUBDOMAIN="${LH_TARGET_SUBDOMAIN:-$INSTANCE_SUBDOMAIN}"
export E2E_RUN_ID="${E2E_RUN_ID:-prod-$(date +%Y%m%d-%H%M%S)-$RANDOM}"

LH_SMOKE_DEVICE_TOKEN_ID=""
LH_SMOKE_DEVICE_ACCESS_TOKEN=""

cleanup_ephemeral_device_token() {
  if [[ -z "$LH_SMOKE_DEVICE_TOKEN_ID" || -z "$LH_SMOKE_DEVICE_ACCESS_TOKEN" ]]; then
    return 0
  fi

  if ! lh_hosted_revoke_device_token "$LH_SMOKE_DEVICE_ACCESS_TOKEN" "$LH_SMOKE_DEVICE_TOKEN_ID" "$API_URL" >/dev/null 2>&1; then
    echo "Warning: failed to revoke ephemeral hosted QA device token $LH_SMOKE_DEVICE_TOKEN_ID" >&2
  fi
}

if [[ -z "${LONGHOUSE_DEVICE_TOKEN:-}" && -n "${CONTROL_PLANE_ADMIN_TOKEN:-${ADMIN_TOKEN:-}}" ]]; then
  if [[ -z "${LH_INSTANCE_ID:-}" || "${LH_INSTANCE_SUBDOMAIN:-}" != "$INSTANCE_SUBDOMAIN" ]]; then
    lh_hosted_resolve_instance "$INSTANCE_SUBDOMAIN"
  fi

  echo "Provisioning ephemeral hosted QA device token for $INSTANCE_SUBDOMAIN..." >&2
  LH_SMOKE_DEVICE_ACCESS_TOKEN="$(lh_hosted_exchange_login_token "$(lh_hosted_issue_login_token "$LH_INSTANCE_ID")" "$API_URL")"
  IFS=$'\t' read -r LH_SMOKE_DEVICE_TOKEN_ID LONGHOUSE_DEVICE_TOKEN <<< \
    "$(lh_hosted_create_device_token "$LH_SMOKE_DEVICE_ACCESS_TOKEN" "$API_URL" "qa-live-${INSTANCE_SUBDOMAIN}-${RANDOM}")"
  export LONGHOUSE_DEVICE_TOKEN
  trap cleanup_ephemeral_device_token EXIT
fi

SMOKE_LOGIN_TOKEN="${SMOKE_LOGIN_TOKEN:-$(lh_hosted_resolved_login_token "$INSTANCE_SUBDOMAIN")}"

export PLAYWRIGHT_BASE_URL="$FRONTEND_URL"
export PLAYWRIGHT_API_BASE_URL="$API_URL"
export PLAYWRIGHT_FRONTEND_BASE="$FRONTEND_URL"
export PLAYWRIGHT_BACKEND_URL="$API_URL"
export FRONTEND_URL="$FRONTEND_URL"
export API_URL="$API_URL"
export RUN_LIVE_E2E="1"
export SMOKE_LOGIN_TOKEN

cd "$ROOT_DIR/e2e"

bunx playwright test --config playwright.prod.config.js "$@"
