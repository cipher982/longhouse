#!/bin/bash

# Production Smoke Test for Swarmlet (Split Deployment)
# Tests frontend (swarmlet.com) and backend (api.swarmlet.com) separately
#
# Usage:
#   ./scripts/smoke-prod.sh              # Full test
#   ./scripts/smoke-prod.sh --wait       # Wait 90s then test (for post-deploy)
#   ./scripts/smoke-prod.sh --quick      # Quick health check only
#
# Environment:
#   SMOKE_TEST_SECRET  - Service account secret for authenticated tests
#   SMOKE_TEST_CHAT    - Set to 1 to enable chat test (costs LLM tokens)

set -e

# Configuration - split deployment
FRONTEND_URL="${FRONTEND_URL:-https://swarmlet.com}"
API_URL="${API_URL:-https://api.swarmlet.com}"
WAIT_SECS="${WAIT_SECS:-90}"

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

pass() { echo -e "  ${GREEN}✓${NC} $1"; PASSED=$((PASSED + 1)); }
fail() { echo -e "  ${RED}✗${NC} $1"; FAILED=$((FAILED + 1)); }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; WARNINGS=$((WARNINGS + 1)); }
info() { echo -e "  ${BLUE}ℹ${NC} $1"; }

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

    local status
    status=$(curl -s -o /dev/null -w "%{http_code}" -b "$cookie_jar" "$url" 2>/dev/null || echo "000")

    if [[ "$status" == "$expected" ]]; then
        pass "$name ($status)"
        return 0
    else
        fail "$name (expected $expected, got $status)"
        return 1
    fi
}

# Test chat sends message and gets AI response
test_chat() {
    local name="$1"
    local cookie_jar="$2"
    local message="${3:-Say hello in exactly 3 words}"
    local timeout_secs="${4:-30}"

    local msg_id
    msg_id=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "smoke-$(date +%s)")

    # Send chat request, capture SSE stream with timeout
    local response
    response=$(timeout "$timeout_secs" curl -s -N -X POST "$API_URL/api/jarvis/chat" \
        -b "$cookie_jar" \
        -H "Content-Type: application/json" \
        -d "{\"message\": \"$message\", \"message_id\": \"$msg_id\"}" 2>/dev/null) || true

    # Check for supervisor_complete event
    if ! echo "$response" | grep -q "event: supervisor_complete"; then
        fail "$name (no supervisor_complete event)"
        return 1
    fi

    # Extract the data line after supervisor_complete
    local complete_data
    complete_data=$(echo "$response" | grep -A1 "event: supervisor_complete" | grep "^data:" | head -1 | sed 's/^data: //')

    if [[ -z "$complete_data" ]]; then
        fail "$name (no data in supervisor_complete)"
        return 1
    fi

    # Check status is success
    local status
    status=$(echo "$complete_data" | jq -r '.payload.status // "unknown"' 2>/dev/null)
    if [[ "$status" != "success" ]]; then
        fail "$name (status: $status)"
        return 1
    fi

    # Check result is non-empty
    local result
    result=$(echo "$complete_data" | jq -r '.payload.result // ""' 2>/dev/null)
    if [[ -z "$result" ]]; then
        fail "$name (empty result)"
        return 1
    fi

    # Show truncated response
    local preview="${result:0:50}"
    [[ ${#result} -gt 50 ]] && preview="${preview}..."
    pass "$name (\"$preview\")"
    return 0
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
    api_url=$(echo "$config" | grep -o 'API_BASE_URL = "[^"]*"' | sed 's/.*"\([^"]*\)".*/\1/')

    if [[ "$api_url" == *"api.swarmlet.com"* ]]; then
        pass "Runtime config: API_BASE_URL = $api_url"
        return 0
    else
        fail "Runtime config: API_BASE_URL incorrect ($api_url)"
        return 1
    fi
}

# Check Caddy for errors
test_caddy() {
    local errors
    # grep -c returns exit code 1 if no matches (but still outputs "0")
    errors=$(ssh zerg "docker logs coolify-proxy 2>&1 | tail -50 | grep -c 'ambiguous site definition'" 2>/dev/null) || true
    # Trim whitespace
    errors=$(echo "$errors" | tr -d '[:space:]')

    if [[ -z "$errors" ]]; then
        warn "Could not check Caddy (SSH failed)"
        return 1
    elif [[ "$errors" == "0" ]]; then
        pass "Caddy: No ambiguous site errors"
        return 0
    else
        fail "Caddy: $errors ambiguous site errors in recent logs"
        return 1
    fi
}

# Parse args
QUICK=0
WAIT=0
while [[ $# -gt 0 ]]; do
    case $1 in
        --quick) QUICK=1; shift ;;
        --wait) WAIT=1; shift ;;
        *) shift ;;
    esac
done

echo ""
echo "================================================"
echo "  Swarmlet Production Smoke Test"
echo "================================================"
echo "  Frontend: $FRONTEND_URL"
echo "  API:      $API_URL"
echo "================================================"
echo ""

# Wait if requested
if [[ $WAIT -eq 1 ]]; then
    echo -e "${YELLOW}Waiting ${WAIT_SECS}s for deployment to stabilize...${NC}"
    sleep "$WAIT_SECS"
    echo ""
fi

# === Quick mode: just health checks ===
if [[ $QUICK -eq 1 ]]; then
    echo "--- Quick Health Check ---"
    test_http "API health" "$API_URL/health" "200"
    test_http "Frontend" "$FRONTEND_URL" "200"
    echo ""
    echo "================================================"
    echo -e "  Quick: ${GREEN}$PASSED passed${NC}, ${RED}$FAILED failed${NC}"
    echo "================================================"
    exit $FAILED
fi

# === Full test suite ===

echo "--- Backend API ($API_URL) ---"
test_http "Health endpoint" "$API_URL/health" "200"
test_json "Health status" "$API_URL/health" ".status" "healthy"
test_json "Auth enabled" "$API_URL/health" ".checks.environment.auth_enabled" "true"
test_json "DB connected" "$API_URL/health" ".checks.database.status" "pass"

echo ""
echo "--- CORS (cross-origin from frontend) ---"
test_cors "Auth endpoint" "$API_URL/api/auth/google" "$FRONTEND_URL"
test_cors "Jarvis endpoint" "$API_URL/api/jarvis/chat" "$FRONTEND_URL"

echo ""
echo "--- Auth (should require login) ---"
test_http "Auth verify (no session)" "$API_URL/api/auth/verify" "401"
test_http "Users/me (no auth)" "$API_URL/api/users/me" "401"

echo ""
echo "--- Jarvis API (should require auth) ---"
test_http "Jarvis bootstrap" "$API_URL/api/jarvis/bootstrap" "401"
test_http "Jarvis agents" "$API_URL/api/jarvis/agents" "401"
test_http "Jarvis history" "$API_URL/api/jarvis/history" "401"

echo ""
echo "--- Frontend ($FRONTEND_URL) ---"
test_http "Landing page" "$FRONTEND_URL" "200"
test_http "Chat page" "$FRONTEND_URL/chat" "200"
test_http "Dashboard" "$FRONTEND_URL/dashboard" "200"
test_config

echo ""
echo "--- Infrastructure ---"
test_caddy

# Authenticated tests (requires SMOKE_TEST_SECRET)
if [[ -n "$SMOKE_TEST_SECRET" ]]; then
    echo ""
    echo "--- Authenticated Flow ---"
    COOKIE_JAR=$(mktemp)

    # Get session - verify login succeeded
    LOGIN_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API_URL/api/auth/service-login" \
        -H "X-Service-Secret: $SMOKE_TEST_SECRET" \
        -c "$COOKIE_JAR")

    if [[ "$LOGIN_STATUS" == "200" ]]; then
        pass "Service login ($LOGIN_STATUS)"
        test_http_auth "Jarvis bootstrap (authed)" "$API_URL/api/jarvis/bootstrap" "200" "$COOKIE_JAR"
        test_http_auth "Jarvis history (authed)" "$API_URL/api/jarvis/history" "200" "$COOKIE_JAR"
        test_http_auth "User profile (authed)" "$API_URL/api/users/me" "200" "$COOKIE_JAR"

        # Chat test (requires LLM - costs money but validates full flow)
        if [[ -n "$SMOKE_TEST_CHAT" ]]; then
            test_chat "Chat sends/receives" "$COOKIE_JAR" "Say hello in exactly 3 words" 30
        else
            warn "SMOKE_TEST_CHAT not set - skipping chat test (costs money)"
        fi
    else
        fail "Service login (got $LOGIN_STATUS)"
    fi

    rm -f "$COOKIE_JAR"
else
    echo ""
    warn "SMOKE_TEST_SECRET not set - skipping authenticated tests"
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
