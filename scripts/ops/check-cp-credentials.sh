#!/usr/bin/env bash
# check-cp-credentials.sh — validate Stripe and SES credentials for the control plane
#
# Usage:
#   ./scripts/check-cp-credentials.sh
#
# Reads credentials from env vars. If not set and coolify binary is available,
# attempts to read from the deployed control plane app.
#
# Exit codes:
#   0 — all configured credentials are valid
#   1 — one or more credentials are invalid or unreachable
set -euo pipefail

PASS=0
FAIL=0

# ANSI colors (safe to use on cube/CI)
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $*"; }
fail() { echo -e "${RED}✗${NC} $*"; FAIL=$((FAIL + 1)); }
warn() { echo -e "${YELLOW}!${NC} $*"; }

# ------------------------------------------------------------------
# Resolve credentials: env var → coolify → skip
# ------------------------------------------------------------------

_resolve_coolify_app_uuid() {
    local app_ref="$1"

    if [[ "$app_ref" =~ ^[a-z0-9]{24}$ ]]; then
        echo "$app_ref"
        return
    fi

    coolify app list --format json 2>/dev/null \
        | python3 -c '
import json
import sys

target = sys.argv[1]
raw = sys.stdin.read()
json_start = raw.find("[")
if json_start == -1:
    raise SystemExit(0)

try:
    rows = json.loads(raw[json_start:])
except Exception:
    raise SystemExit(0)

for row in rows:
    if row.get("name") == target:
        print(row.get("uuid", ""))
        raise SystemExit(0)
' "$app_ref"
}

_get_env_or_coolify() {
    local var_name="$1"
    local coolify_app="${2:-longhouse-control-plane}"
    local coolify_key="${3:-$var_name}"
    local coolify_app_id=""
    local value="${!var_name:-}"

    if [ -n "$value" ]; then
        echo "$value"
        return
    fi

    # Try coolify if available (cube runner)
    if command -v coolify &>/dev/null; then
        coolify_app_id="$(_resolve_coolify_app_uuid "$coolify_app")"
        if [ -z "$coolify_app_id" ]; then
            echo ""
            return
        fi

        value=$(coolify app env list "$coolify_app_id" --format json -s 2>/dev/null \
            | python3 -c '
import json
import sys

target_key = sys.argv[1]
raw = sys.stdin.read()
json_start = raw.find("[")
if json_start == -1:
    raise SystemExit(0)

try:
    rows = json.loads(raw[json_start:])
except Exception:
    raise SystemExit(0)

for row in rows:
    if row.get("key") != target_key:
        continue
    value = row.get("real_value") or row.get("value") or ""
    if value == "********":
        continue
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("\"", chr(39)):
        value = value[1:-1]
    print(value)
    raise SystemExit(0)
' "$coolify_key")
        if [ -n "$value" ]; then
            echo "$value"
            return
        fi
    fi

    echo ""
}

# ------------------------------------------------------------------
# Stripe check
# ------------------------------------------------------------------

check_stripe() {
    local key
    key=$(_get_env_or_coolify "STRIPE_SECRET_KEY" "longhouse-control-plane" "CONTROL_PLANE_STRIPE_SECRET_KEY")

    if [ -z "$key" ]; then
        warn "STRIPE_SECRET_KEY not set — skipping Stripe check"
        return
    fi

    echo "Checking Stripe key..."
    local http_status
    http_status=$(curl -s -o /dev/null -w "%{http_code}" \
        --max-time 10 \
        -H "Authorization: Bearer ${key}" \
        https://api.stripe.com/v1/balance)

    if [ "$http_status" = "200" ]; then
        ok "Stripe key valid (HTTP 200)"
        PASS=$((PASS + 1))
    else
        fail "Stripe key invalid or expired (HTTP ${http_status}) — billing will fail"
    fi
}

# ------------------------------------------------------------------
# SES check
# ------------------------------------------------------------------

check_ses() {
    local access_key secret_key region

    access_key=$(_get_env_or_coolify "CONTROL_PLANE_INSTANCE_AWS_SES_ACCESS_KEY_ID")
    secret_key=$(_get_env_or_coolify "CONTROL_PLANE_INSTANCE_AWS_SES_SECRET_ACCESS_KEY")
    region=$(_get_env_or_coolify "CONTROL_PLANE_INSTANCE_AWS_SES_REGION")
    region="${region:-us-east-1}"

    if [ -z "$access_key" ] || [ -z "$secret_key" ]; then
        warn "SES credentials not set — skipping SES check"
        return
    fi

    if ! command -v aws &>/dev/null; then
        warn "aws CLI not found — skipping SES check"
        return
    fi

    echo "Checking SES credentials..."
    local quota
    if quota=$(AWS_ACCESS_KEY_ID="$access_key" \
               AWS_SECRET_ACCESS_KEY="$secret_key" \
               AWS_DEFAULT_REGION="$region" \
               aws ses get-send-quota --output text \
                   --cli-connect-timeout 5 --cli-read-timeout 5 2>&1); then
        ok "SES credentials valid — quota: ${quota}"
        PASS=$((PASS + 1))
    else
        fail "SES credentials invalid — email will fail: ${quota}"
    fi
}

# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

echo "=== Control Plane Credential Check ==="
echo ""

check_stripe
check_ses

echo ""
echo "=== Results: ${PASS} passed, ${FAIL} failed ==="

if [ "$FAIL" -gt 0 ]; then
    exit 1
fi
exit 0
