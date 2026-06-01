#!/bin/bash

# Production Smoke Test for Longhouse (split frontend/backend)
#
# Usage:
#   ./scripts/smoke-prod.sh           # default: public demo + auth + basic LLM
#   ./scripts/smoke-prod.sh --quick   # public health only
#   ./scripts/smoke-prod.sh --full    # default + CRUD + infra
#   ./scripts/smoke-prod.sh --no-llm  # skip LLM capability check
#   ./scripts/smoke-prod.sh --wait    # wait 90s then test (post-deploy)
#
# Environment:
#   INSTANCE_SUBDOMAIN        - Hosted instance subdomain (defaults to $LONGHOUSE_DEFAULT_SUBDOMAIN or demo when control-plane auth is configured)
#   CONTROL_PLANE_URL         - Control-plane base URL for hosted instance resolution
#   CONTROL_PLANE_ADMIN_TOKEN - Admin token for hosted control-plane resolution (or set direct FRONTEND_URL/API_URL instead)
#   FRONTEND_URL / API_URL    - Optional direct URL overrides when not resolving via control plane

set -e

# Load repo .env if present (local only; no auto-creation)
ROOT_DIR="$(cd "$(dirname "$0")/../.." && pwd)"
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

if ! command -v jq >/dev/null 2>&1; then
    echo "Missing jq. Install it first (brew install jq)." >&2
    exit 1
fi

INSTANCE_SUBDOMAIN="${INSTANCE_SUBDOMAIN:-}"
FRONTEND_URL="${FRONTEND_URL:-}"
API_URL="${API_URL:-$FRONTEND_URL}"
BROWSER_TIMELINE_SESSIONS_PATH="/api/timeline/sessions?limit=1"

lh_hosted_prepare_target "$INSTANCE_SUBDOMAIN" "$FRONTEND_URL" "$API_URL" "${LONGHOUSE_DEFAULT_SUBDOMAIN:-demo}"
FRONTEND_URL="$LH_TARGET_FRONTEND_URL"
API_URL="$LH_TARGET_API_URL"
INSTANCE_SUBDOMAIN="${LH_TARGET_SUBDOMAIN:-$INSTANCE_SUBDOMAIN}"

PUBLIC_DEMO_URL="${PUBLIC_DEMO_URL:-${MARKETING_URL:-https://longhouse.ai}}"
CONTROL_PLANE_URL="${CONTROL_PLANE_URL:-${CP_URL:-https://control.longhouse.ai}}"
CP_URL="${CP_URL:-$CONTROL_PLANE_URL}"
WAIT_SECS="${WAIT_SECS:-90}"
INSTANCE_AUTH_ENABLED="unknown"

# Counters
PASSED=0
FAILED=0
WARNINGS=0

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Detect timeout command (GNU timeout not available on macOS)
if command -v timeout &> /dev/null; then
    TIMEOUT_CMD="timeout"
elif command -v gtimeout &> /dev/null; then
    TIMEOUT_CMD="gtimeout"
else
    # Fallback: no timeout (warn user)
    TIMEOUT_CMD=""
fi

pass() { echo -e "  ${GREEN}✓${NC} $1"; PASSED=$((PASSED + 1)); }
fail() { echo -e "  ${RED}✗${NC} $1"; FAILED=$((FAILED + 1)); }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; WARNINGS=$((WARNINGS + 1)); }
info() { echo -e "  ${BLUE}ℹ${NC} $1"; }

new_message_id() {
    (uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "smoke-$(date +%s)") | tr '[:upper:]' '[:lower:]'
}

section() {
    echo ""
    echo "--- $1 ---"
}

# Wait for API readiness to return a healthy/ready status
# Polls /api/readyz until status is healthy/ok/ready three times consecutively (or timeout)
wait_for_health() {
    local url="${1:-$API_URL/api/readyz}"
    local max_attempts="${2:-45}"   # 45 * 2s = 90s max
    local interval="${3:-2}"
    local required_consecutive=3

    local attempt=0
    local consecutive_ok=0

    echo -e "${YELLOW}Waiting for API health...${NC}"

    while [[ $attempt -lt $max_attempts ]]; do
        attempt=$((attempt + 1))

        local response
        response=$(curl -s --max-time 5 "$url" 2>/dev/null || echo '{}')

        local status
        status=$(echo "$response" | jq -r '.status // "unknown"' 2>/dev/null)

        if [[ "$status" == "healthy" || "$status" == "ok" || "$status" == "ready" ]]; then
            consecutive_ok=$((consecutive_ok + 1))
            if [[ $consecutive_ok -ge $required_consecutive ]]; then
                echo -e "${GREEN}✓${NC} API ready after $attempt attempts"
                return 0
            fi
        else
            consecutive_ok=0
        fi

        # Show progress every 5 attempts
        if [[ $((attempt % 5)) -eq 0 ]]; then
            echo -e "  ${BLUE}...${NC} attempt $attempt/$max_attempts (status: $status)"
        fi

        sleep "$interval"
    done

    echo -e "${YELLOW}⚠${NC} Health check timeout after $attempt attempts - proceeding anyway"
    return 0  # Don't fail - let tests run and report actual failures
}

# Run a test and keep going even if it fails (so we can report a full summary).
run_test() {
    "$@" || true
}

# Test HTTP endpoint
test_http() {
    local name="$1"
    local url="$2"
    local expected="$3"
    local method="${4:-GET}"
    local data="${5:-}"

    local args="-s -o /dev/null -w %{http_code}"
    [[ "$method" != "GET" ]] && args="$args -X $method"
    [[ -n "$data" ]] && args="$args -H 'Content-Type: application/json' -d '$data'"

    local status
    status=$(eval "curl $args '$url'" 2>/dev/null || echo "000")

    if [[ "$status" == "$expected" ]]; then
        pass "$name ($status)"
        return 0
    else
        fail "$name (expected $expected, got $status)"
        return 1
    fi
}

# Test HTTP with cookie auth
test_http_auth() {
    local name="$1"
    local url="$2"
    local expected="$3"
    local cookie_jar="$4"
    local method="${5:-GET}"
    local data="${6:-}"

    local args="-s -o /dev/null -w %{http_code} -b $cookie_jar"
    [[ "$method" != "GET" ]] && args="$args -X $method"
    [[ -n "$data" ]] && args="$args -H 'Content-Type: application/json' -d '$data'"

    local status
    status=$(eval "curl $args '$url'" 2>/dev/null || echo "000")

    if [[ "$status" == "$expected" ]]; then
        pass "$name ($status)"
        return 0
    else
        fail "$name (expected $expected, got $status)"
        return 1
    fi
}

# Test JSON field
test_json() {
    local name="$1"
    local url="$2"
    local jq_path="$3"
    local expected="$4"

    local value
    value=$(curl -s "$url" 2>/dev/null | jq -r "$jq_path" 2>/dev/null || echo "ERROR")

    if [[ "$value" == "$expected" ]]; then
        pass "$name ($jq_path = $value)"
        return 0
    else
        fail "$name ($jq_path expected '$expected', got '$value')"
        return 1
    fi
}

test_json_eventually() {
    local name="$1"
    local url="$2"
    local jq_path="$3"
    local expected="$4"
    local max_attempts="${5:-15}"
    local interval="${6:-2}"

    local attempt=0
    local value="unknown"
    while [[ $attempt -lt $max_attempts ]]; do
        attempt=$((attempt + 1))
        value=$(curl -s "$url" 2>/dev/null | jq -r "$jq_path" 2>/dev/null || echo "ERROR")
        if [[ "$value" == "$expected" ]]; then
            pass "$name ($jq_path = $value after $attempt attempts)"
            return 0
        fi
        sleep "$interval"
    done

    fail "$name ($jq_path expected '$expected', got '$value' after $max_attempts attempts)"
    return 1
}

# Test CORS preflight
test_cors() {
    local name="$1"
    local url="$2"
    local origin="$3"

    local allow_origin
    allow_origin=$(curl -s -I -X OPTIONS "$url" \
        -H "Origin: $origin" \
        -H "Access-Control-Request-Method: POST" 2>/dev/null | \
        grep -i "access-control-allow-origin" | tr -d '\r' | awk '{print $2}')

    if [[ "$allow_origin" == "$origin" ]]; then
        pass "$name CORS allows $origin"
        return 0
    else
        fail "$name CORS does not allow $origin (got: $allow_origin)"
        return 1
    fi
}

# Test runtime config
test_config() {
    local config
    config=$(curl -s "$FRONTEND_URL/config.js" 2>/dev/null)

    local api_url
    api_url=$(echo "$config" | grep -o 'API_BASE_URL *= *"[^"]*"' | sed 's/.*"\([^"]*\)".*/\1/')

    local api_expected
    local api_expected_with_suffix
    api_expected="${API_URL%/}"
    api_expected_with_suffix="${api_expected}/api"

    if [[ "$api_url" == "$api_expected" || "$api_url" == "$api_expected_with_suffix" ]]; then
        pass "Runtime config: API_BASE_URL = $api_url"
        return 0
    fi

    # Same-origin deployments use a relative /api base.
    if [[ "$api_url" == "/api" && "${API_URL%/}" == "${FRONTEND_URL%/}" ]]; then
        pass "Runtime config: API_BASE_URL = $api_url"
        return 0
    else
        fail "Runtime config: API_BASE_URL incorrect ($api_url)"
        return 1
    fi
}

# Check Caddy for errors (optional - requires SSH access)
test_caddy() {
    local errors
    # grep -c returns exit code 1 if no matches (but still outputs "0")
    errors=$(ssh runtime-host "docker logs coolify-proxy 2>&1 | tail -50 | grep -c 'ambiguous site definition'" 2>/dev/null) || true
    # Trim whitespace
    errors=$(echo "$errors" | tr -d '[:space:]')

    if [[ -z "$errors" ]]; then
        warn "Could not check Caddy (SSH not available)"
        return 0  # Don't fail on missing SSH - it's optional
    elif [[ "$errors" == "0" ]]; then
        pass "Caddy: No ambiguous site errors"
        return 0
    else
        fail "Caddy: $errors ambiguous site errors in recent logs"
        return 1
    fi
}

run_health_checks() {
    run_test test_http "API health" "$API_URL/api/health" "200"
    run_test test_json "Health status" "$API_URL/api/health" ".status" "healthy"
    # /api/health hides per-check internals from unauthenticated callers, so read
    # auth state from the public /api/system/info (auth_disabled) instead of
    # .checks.environment.auth_enabled, and verify DB readiness via /api/readyz
    # (the purpose-built probe that 503s when the DB is unavailable).
    INSTANCE_AUTH_DISABLED=$(curl -s "$API_URL/api/system/info" 2>/dev/null | jq -r '.auth_disabled // "unknown"' 2>/dev/null)
    if [[ "$INSTANCE_AUTH_DISABLED" == "false" ]]; then
        pass "Auth enabled (true)"
        INSTANCE_AUTH_ENABLED="true"
    elif [[ "$INSTANCE_AUTH_DISABLED" == "true" ]]; then
        pass "Auth enabled (false)"
        INSTANCE_AUTH_ENABLED="false"
    else
        warn "Auth enabled state unknown ($INSTANCE_AUTH_DISABLED)"
        INSTANCE_AUTH_ENABLED="unknown"
    fi
    run_test test_http "DB ready (readyz)" "$API_URL/api/readyz" "200"
}

run_frontend_checks() {
    run_test test_http "Landing page" "$FRONTEND_URL" "200"
    run_test test_http "Chat page" "$FRONTEND_URL/chat" "200"
    run_test test_http "Dashboard" "$FRONTEND_URL/dashboard" "200"
    run_test test_http "Swarm Ops" "$FRONTEND_URL/swarm" "200"
    run_test test_config
}

run_cross_service_checks() {
    # Public demo runtime
    run_test test_http "Public demo runtime" "$PUBLIC_DEMO_URL" "200"
    run_test test_http "Public demo API health" "$PUBLIC_DEMO_URL/api/health" "200"

    # Control plane health
    run_test test_http "CP health endpoint" "$CP_URL/health" "200"
    run_test test_json_eventually "CP health status" "$CP_URL/health" ".status" "ok"

    # Public demo JS references control plane URL
    local js_urls
    js_urls=$(curl -s "$PUBLIC_DEMO_URL" 2>/dev/null | grep -oE 'src="/assets/[^"]+\.js"' | sed 's/src="//;s/"//' || true)
    if [[ -n "$js_urls" ]]; then
        local found_cp=0
        for js_path in $js_urls; do
            local js_content
            js_content=$(curl -s "${PUBLIC_DEMO_URL}${js_path}" 2>/dev/null || true)
            if grep -q "control.longhouse.ai" <<< "$js_content"; then
                found_cp=1
                break
            fi
        done
        if [[ $found_cp -eq 1 ]]; then
            pass "Public demo JS contains CP URL"
        else
            warn "Public demo JS does not reference control.longhouse.ai"
        fi
    else
        warn "No JS bundles found on public demo runtime"
    fi
}

run_cors_checks() {
    if [[ "${API_URL%/}" == "${FRONTEND_URL%/}" ]]; then
        info "CORS checks skipped (same-origin deployment)"
        return 0
    fi

    run_test test_cors "Auth endpoint" "$API_URL/api/auth/google" "$FRONTEND_URL"
}

run_auth_gate_checks() {
    run_test test_http "Auth verify (no session)" "$API_URL/api/auth/verify" "401"
    run_test test_http "Users/me (no auth)" "$API_URL/api/users/me" "401"
    run_test test_http "Email contacts (no auth)" "$API_URL/api/user/contacts/email" "401"
    run_test test_http "Phone contacts (no auth)" "$API_URL/api/user/contacts/phone" "401"
}

run_contacts_crud() {
    local cookie_jar="$1"

    run_test test_http_auth "List email contacts" "$API_URL/api/user/contacts/email" "200" "$cookie_jar"
    run_test test_http_auth "List phone contacts" "$API_URL/api/user/contacts/phone" "200" "$cookie_jar"

    local email_response
    local email_id
    email_response=$(curl -s -X POST "$API_URL/api/user/contacts/email" \
        -b "$cookie_jar" \
        -H "Content-Type: application/json" \
        -d '{"name": "Smoke Test", "email": "smoke-test@example.com", "notes": "Created by smoke test"}' 2>/dev/null)
    email_id=$(echo "$email_response" | jq -r '.id // empty' 2>/dev/null)
    if [[ -n "$email_id" && "$email_id" != "null" ]]; then
        pass "Create email contact (id: $email_id)"
    else
        fail "Create email contact (no id returned)"
    fi

    local phone_response
    local phone_id
    phone_response=$(curl -s -X POST "$API_URL/api/user/contacts/phone" \
        -b "$cookie_jar" \
        -H "Content-Type: application/json" \
        -d '{"name": "Smoke Test Phone", "phone": "+15551234567", "notes": "Created by smoke test"}' 2>/dev/null)
    phone_id=$(echo "$phone_response" | jq -r '.id // empty' 2>/dev/null)
    if [[ -n "$phone_id" && "$phone_id" != "null" ]]; then
        pass "Create phone contact (id: $phone_id)"
    else
        fail "Create phone contact (no id returned)"
    fi

    if [[ -n "$email_id" && "$email_id" != "null" ]]; then
        local delete_status
        delete_status=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
            "$API_URL/api/user/contacts/email/$email_id" -b "$cookie_jar" 2>/dev/null)
        if [[ "$delete_status" == "204" ]]; then
            pass "Delete email contact (204)"
        else
            fail "Delete email contact (expected 204, got $delete_status)"
        fi
    fi

    if [[ -n "$phone_id" && "$phone_id" != "null" ]]; then
        local delete_status
        delete_status=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
            "$API_URL/api/user/contacts/phone/$phone_id" -b "$cookie_jar" 2>/dev/null)
        if [[ "$delete_status" == "204" ]]; then
            pass "Delete phone contact (204)"
        else
            fail "Delete phone contact (expected 204, got $delete_status)"
        fi
    fi
}

# Parse args
MODE="default" # quick | default | full
QUICK=0
FULL=0
WAIT=0
RUN_LLM=1
while [[ $# -gt 0 ]]; do
    case $1 in
        --quick) QUICK=1; MODE="quick"; shift ;;
        --full) FULL=1; MODE="full"; shift ;;
        --no-llm) RUN_LLM=0; shift ;;
        --wait) WAIT=1; shift ;;
        --chat) FULL=1; MODE="full"; info "--chat is deprecated; use --full"; shift ;;
        -h|--help)
            echo "Usage: ./scripts/smoke-prod.sh [--quick] [--full] [--no-llm] [--wait]"
            exit 0
            ;;
        *) shift ;;
    esac
done

if [[ $QUICK -eq 1 && $FULL -eq 1 ]]; then
    info "--quick ignores --full"
    MODE="quick"
fi

echo ""
echo "================================================"
echo "  Longhouse Production Smoke Test"
echo "================================================"
echo "  Frontend:  $FRONTEND_URL"
echo "  API:       $API_URL"
echo "  Demo:      $PUBLIC_DEMO_URL"
echo "  CP:        $CP_URL"
echo "================================================"
echo ""

# Wait if requested - polls health instead of static sleep
if [[ $WAIT -eq 1 ]]; then
    wait_for_health "$API_URL/api/readyz"
    echo ""
fi

# === Quick mode: just health checks ===
if [[ "$MODE" == "quick" ]]; then
    section "Health (quick)"
    run_test test_http "API health" "$API_URL/api/health" "200"
    run_test test_http "Frontend" "$FRONTEND_URL" "200"
    echo ""
    echo "================================================"
    echo -e "  Quick: ${GREEN}$PASSED passed${NC}, ${RED}$FAILED failed${NC}"
    echo "================================================"
    exit $FAILED
fi

# === Default smoke ===
section "Health"
run_health_checks

section "Frontend"
run_frontend_checks

section "Cross-Service"
run_cross_service_checks

section "CORS"
run_cors_checks

section "Auth gates (unauthenticated)"
run_auth_gate_checks

# Authenticated tests (hosted login-token flow when auth is enabled)
if [[ "$INSTANCE_AUTH_ENABLED" == "true" ]]; then
    if [[ -z "$INSTANCE_SUBDOMAIN" ]]; then
        echo ""
        warn "Auth enabled but INSTANCE_SUBDOMAIN is not set - skipping authenticated + LLM tests"
    else
        section "Authenticated"
        COOKIE_JAR=$(mktemp)

        if lh_hosted_authenticate_cookie_jar "$INSTANCE_SUBDOMAIN" "$COOKIE_JAR"; then
            pass "Hosted login token accepted"
            run_test test_http_auth "User profile (authed)" "$API_URL/api/users/me" "200" "$COOKIE_JAR"
            # Browser-auth smoke must stay on the browser-owned timeline API.
            run_test test_http_auth "Timeline sessions (authed)" "$API_URL$BROWSER_TIMELINE_SESSIONS_PATH" "200" "$COOKIE_JAR"

            if [[ $RUN_LLM -eq 1 ]]; then
                llm_available=$(curl -s "$API_URL/api/system/capabilities" 2>/dev/null | jq -r '.llm_available // "unknown"' 2>/dev/null)
                if [[ "$llm_available" != "true" ]]; then
                    warn "LLM unavailable (llm_available=$llm_available) - skipping LLM tests"
                else
                    info "LLM available but legacy chat smoke path removed; no LLM smoke tests"
                fi
            else
                info "LLM test skipped (--no-llm)"
            fi

            if [[ "$MODE" == "full" ]]; then
                section "Contacts CRUD"
                run_contacts_crud "$COOKIE_JAR"

                info "Email/Gmail canaries removed with the legacy chat path"
            else
                info "Full tests skipped (pass --full to enable CRUD/infra)"
            fi
        else
            fail "Hosted login token auth failed"
        fi

        rm -f "$COOKIE_JAR"
    fi
elif [[ "$INSTANCE_AUTH_ENABLED" == "false" ]]; then
    echo ""
    info "Auth disabled on target - skipping authenticated + LLM tests"
else
    echo ""
    warn "Auth state unknown - skipping authenticated + LLM tests"
fi


if [[ "$MODE" == "full" ]]; then
    section "Infrastructure"
    run_test test_caddy
fi

# Summary
echo ""
echo "================================================"
if [[ $FAILED -eq 0 ]]; then
    echo -e "  ${GREEN}All $PASSED tests passed!${NC}"
    [[ $WARNINGS -gt 0 ]] && echo -e "  ${YELLOW}$WARNINGS warnings${NC}"
else
    echo -e "  ${RED}$FAILED failed${NC}, ${GREEN}$PASSED passed${NC}, ${YELLOW}$WARNINGS warnings${NC}"
fi
echo "================================================"
exit $FAILED
