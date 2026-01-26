#!/bin/bash

# Production Smoke Test for Swarmlet (split frontend/backend)
#
# Usage:
#   ./scripts/smoke-prod.sh           # default: public + auth + basic LLM
#   ./scripts/smoke-prod.sh --quick   # public health only
#   ./scripts/smoke-prod.sh --full    # default + CRUD + email + infra
#   ./scripts/smoke-prod.sh --no-llm  # skip LLM chat test
#   ./scripts/smoke-prod.sh --wait    # wait 90s then test (post-deploy)
#
# Environment:
#   SMOKE_TEST_SECRET  - Service account secret for authenticated tests
#   SMOKE_TEST_EMAIL   - Email target for email tool test (default: david010@gmail.com)
#   SMOKE_RUN_ID       - Optional run id for isolated smoke user/thread

set -e

# Configuration - split deployment
FRONTEND_URL="${FRONTEND_URL:-https://swarmlet.com}"
API_URL="${API_URL:-https://api.swarmlet.com}"
WAIT_SECS="${WAIT_SECS:-90}"
SMOKE_TEST_EMAIL="${SMOKE_TEST_EMAIL:-david010@gmail.com}"
SMOKE_RUN_ID="${SMOKE_RUN_ID:-}"

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

gen_id() {
    uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "smoke-$(date +%s)"
}

pass() { echo -e "  ${GREEN}✓${NC} $1"; PASSED=$((PASSED + 1)); }
fail() { echo -e "  ${RED}✗${NC} $1"; FAILED=$((FAILED + 1)); }
warn() { echo -e "  ${YELLOW}⚠${NC} $1"; WARNINGS=$((WARNINGS + 1)); }
info() { echo -e "  ${BLUE}ℹ${NC} $1"; }

section() {
    echo ""
    echo "--- $1 ---"
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

# Test chat sends message and gets AI response
test_chat() {
    local name="$1"
    local cookie_jar="$2"
    local message="${3:-Reply with the single word OK.}"
    local timeout_secs="${4:-30}"
    local expected_regex="${5:-}"

    local msg_id
    msg_id=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "smoke-$(date +%s)")

    # Send chat request, capture SSE stream with timeout
    local response
    if [[ -n "$TIMEOUT_CMD" ]]; then
        response=$($TIMEOUT_CMD "$timeout_secs" curl -s -N -X POST "$API_URL/api/jarvis/chat" \
            -b "$cookie_jar" \
            -H "Content-Type: application/json" \
            -d "{\"message\": \"$message\", \"message_id\": \"$msg_id\"}" 2>/dev/null) || true
    else
        warn "timeout command not found - chat test may hang (install: brew install coreutils)"
        response=$(curl -s -N -X POST "$API_URL/api/jarvis/chat" \
            -b "$cookie_jar" \
            -H "Content-Type: application/json" \
            -d "{\"message\": \"$message\", \"message_id\": \"$msg_id\"}" 2>/dev/null) || true
    fi

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

    if [[ -n "$expected_regex" ]]; then
        if ! echo "$result" | grep -E -i -q "$expected_regex"; then
            fail "$name (unexpected response)"
            return 1
        fi
    fi

    # Show truncated response
    local preview="${result:0:50}"
    [[ ${#result} -gt 50 ]] && preview="${preview}..."
    pass "$name (\"$preview\")"
    return 0
}

# Test voice turn (STT + supervisor response)
test_voice() {
    local name="$1"
    local cookie_jar="$2"
    local timeout_secs="${3:-30}"

    local msg_id
    msg_id=$(uuidgen 2>/dev/null || cat /proc/sys/kernel/random/uuid 2>/dev/null || echo "smoke-voice-$(date +%s)")

    # Create minimal valid WAV file (44-byte header + 1600 bytes of silence = 100ms at 8kHz 16-bit mono)
    local wav_file
    wav_file=$(mktemp)
    # WAV header (44 bytes) - 8kHz, 16-bit, mono, 1600 samples
    printf 'RIFF' > "$wav_file"
    printf '\x24\x08\x00\x00' >> "$wav_file"  # file size - 8
    printf 'WAVE' >> "$wav_file"
    printf 'fmt ' >> "$wav_file"
    printf '\x10\x00\x00\x00' >> "$wav_file"  # fmt chunk size (16)
    printf '\x01\x00' >> "$wav_file"          # PCM format
    printf '\x01\x00' >> "$wav_file"          # 1 channel
    printf '\x40\x1f\x00\x00' >> "$wav_file"  # 8000 Hz sample rate
    printf '\x80\x3e\x00\x00' >> "$wav_file"  # byte rate (8000 * 2)
    printf '\x02\x00' >> "$wav_file"          # block align
    printf '\x10\x00' >> "$wav_file"          # 16 bits per sample
    printf 'data' >> "$wav_file"
    printf '\x40\x06\x00\x00' >> "$wav_file"  # data size (1600)
    # Silence data (1600 bytes of zeros)
    dd if=/dev/zero bs=1600 count=1 >> "$wav_file" 2>/dev/null

    # Send voice request
    local response
    if [[ -n "$TIMEOUT_CMD" ]]; then
        response=$($TIMEOUT_CMD "$timeout_secs" curl -s -X POST "$API_URL/api/jarvis/voice/turn" \
            -b "$cookie_jar" \
            -F "audio=@${wav_file};type=audio/wav" \
            -F "return_audio=false" \
            -F "message_id=$msg_id" 2>/dev/null) || true
    else
        response=$(curl -s -X POST "$API_URL/api/jarvis/voice/turn" \
            -b "$cookie_jar" \
            -F "audio=@${wav_file};type=audio/wav" \
            -F "return_audio=false" \
            -F "message_id=$msg_id" 2>/dev/null) || true
    fi

    rm -f "$wav_file"

    if [[ -z "$response" ]]; then
        fail "$name (no response / timeout)"
        return 1
    fi

    # Check message_id passthrough
    local returned_msg_id
    returned_msg_id=$(echo "$response" | jq -r '.message_id // empty' 2>/dev/null)
    if [[ "$returned_msg_id" != "$msg_id" ]]; then
        fail "$name (message_id mismatch: expected $msg_id, got $returned_msg_id)"
        return 1
    fi

    # Check status
    local status
    status=$(echo "$response" | jq -r '.status // empty' 2>/dev/null)
    if [[ "$status" != "success" ]]; then
        local error
        error=$(echo "$response" | jq -r '.error // "unknown"' 2>/dev/null)
        fail "$name (status=$status, error=$error)"
        return 1
    fi

    # Check transcript exists
    local transcript
    transcript=$(echo "$response" | jq -r '.transcript // empty' 2>/dev/null)
    if [[ -z "$transcript" ]]; then
        fail "$name (empty transcript)"
        return 1
    fi

    pass "$name (transcript: \"${transcript:0:30}...\")"
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

    local api_expected
    local api_expected_with_suffix
    api_expected="${API_URL%/}"
    api_expected_with_suffix="${api_expected}/api"

    if [[ "$api_url" == "$api_expected" || "$api_url" == "$api_expected_with_suffix" ]]; then
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
    errors=$(ssh zerg "docker logs coolify-proxy 2>&1 | tail -50 | grep -c 'ambiguous site definition'" 2>/dev/null) || true
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

test_csp_connect() {
    local name="$1"
    local url="$2"
    local expected="$3"

    local headers
    headers=$(curl -s -D - "$url" -o /dev/null 2>/dev/null || true)
    local csp
    csp=$(echo "$headers" | tr -d '\r' | awk -F': ' 'tolower($1)=="content-security-policy" {print $2}' | tail -1)

    if [[ -z "$csp" ]]; then
        fail "$name (missing CSP header)"
        return 1
    fi

    if [[ "$csp" != *"connect-src"* ]]; then
        fail "$name (no connect-src directive)"
        return 1
    fi

    if [[ "$csp" == *"$expected"* ]]; then
        pass "$name (connect-src includes $expected)"
        return 0
    else
        fail "$name (connect-src missing $expected)"
        return 1
    fi
}

run_health_checks() {
    run_test test_http "API health" "$API_URL/health" "200"
    run_test test_json "Health status" "$API_URL/health" ".status" "healthy"
    run_test test_json "Auth enabled" "$API_URL/health" ".checks.environment.auth_enabled" "true"
    run_test test_json "DB connected" "$API_URL/health" ".checks.database.status" "pass"
}

run_frontend_checks() {
    run_test test_http "Landing page" "$FRONTEND_URL" "200"
    run_test test_http "Chat page" "$FRONTEND_URL/chat" "200"
    run_test test_http "Dashboard" "$FRONTEND_URL/dashboard" "200"
    run_test test_http "Swarm Ops" "$FRONTEND_URL/swarm" "200"
    run_test test_config
    run_test test_csp_connect "CSP: OpenAI Realtime" "$FRONTEND_URL" "api.openai.com"
}

run_cors_checks() {
    run_test test_cors "Auth endpoint" "$API_URL/api/auth/google" "$FRONTEND_URL"
    run_test test_cors "Jarvis endpoint" "$API_URL/api/jarvis/chat" "$FRONTEND_URL"
}

run_auth_gate_checks() {
    run_test test_http "Auth verify (no session)" "$API_URL/api/auth/verify" "401"
    run_test test_http "Users/me (no auth)" "$API_URL/api/users/me" "401"
    run_test test_http "Jarvis bootstrap (no auth)" "$API_URL/api/jarvis/bootstrap" "401"
    run_test test_http "Jarvis history (no auth)" "$API_URL/api/jarvis/history" "401"
    run_test test_http "Jarvis agents (no auth)" "$API_URL/api/jarvis/agents" "401"
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

run_email_tool_test() {
    local cookie_jar="$1"
    local timestamp
    timestamp=$(date +%s)

    # Ensure the target email is an approved contact
    curl -s -X POST "$API_URL/api/user/contacts/email" \
        -b "$cookie_jar" \
        -H "Content-Type: application/json" \
        -d '{"name": "Smoke Test Email", "email": "'$SMOKE_TEST_EMAIL'"}' > /dev/null 2>&1 || true

    local payload
    payload="{\"message\": \"send_email to $SMOKE_TEST_EMAIL subject SmokeTest-$timestamp text Automated smoke test\", \"message_id\": \"smoke-email-$timestamp\"}"

    local response
    if [[ -n "$TIMEOUT_CMD" ]]; then
        response=$($TIMEOUT_CMD 60 curl -s -N -X POST "$API_URL/api/jarvis/chat" \
            -b "$cookie_jar" \
            -H "Content-Type: application/json" \
            -d "$payload" 2>/dev/null) || true
    else
        warn "timeout command not found - email test may hang (install: brew install coreutils)"
        response=$(curl -s -N -X POST "$API_URL/api/jarvis/chat" \
            -b "$cookie_jar" \
            -H "Content-Type: application/json" \
            -d "$payload" 2>/dev/null) || true
    fi

    if echo "$response" | grep -q "supervisor_tool_completed"; then
        if echo "$response" | grep -q "Message ID:"; then
            pass "Email tool sent successfully"
        else
            fail "Email tool completed but no message ID"
        fi
    elif echo "$response" | grep -q "supervisor_tool_failed"; then
        local error
        error=$(echo "$response" | grep -o '"error": "[^"]*"' | head -1)
        fail "Email tool failed: $error"
    else
        warn "Email tool test inconclusive"
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
if [[ "$MODE" == "quick" ]]; then
    section "Health (quick)"
    run_test test_http "API health" "$API_URL/health" "200"
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

section "CORS"
run_cors_checks

section "Auth gates (unauthenticated)"
run_auth_gate_checks

# Authenticated tests (requires SMOKE_TEST_SECRET)
if [[ -n "$SMOKE_TEST_SECRET" ]]; then
    section "Authenticated"

    if [[ -z "$SMOKE_RUN_ID" ]]; then
        SMOKE_RUN_ID="smoke-$(gen_id)"
    fi
    info "Smoke run id: $SMOKE_RUN_ID"

    COOKIE_JAR=$(mktemp)
    LOGIN_STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$API_URL/api/auth/service-login" \
        -H "X-Service-Secret: $SMOKE_TEST_SECRET" \
        -H "X-Smoke-Run-Id: $SMOKE_RUN_ID" \
        -c "$COOKIE_JAR")

    if [[ "$LOGIN_STATUS" == "200" ]]; then
        pass "Service login ($LOGIN_STATUS)"
        run_test test_http_auth "Jarvis bootstrap (authed)" "$API_URL/api/jarvis/bootstrap" "200" "$COOKIE_JAR"
        run_test test_http_auth "Jarvis history (authed)" "$API_URL/api/jarvis/history" "200" "$COOKIE_JAR"
        run_test test_http_auth "Jarvis runs (authed)" "$API_URL/api/jarvis/runs?limit=1" "200" "$COOKIE_JAR"
        run_test test_http_auth "User profile (authed)" "$API_URL/api/users/me" "200" "$COOKIE_JAR"

        if [[ $RUN_LLM -eq 1 ]]; then
            section "LLM"
            run_test test_chat "Basic chat (2+2)" "$COOKIE_JAR" "What is 2+2? Reply with just the number." 30 '(^|[^0-9])4($|[^0-9])'
            run_test test_chat "Basic chat (France capital)" "$COOKIE_JAR" "What is the capital of France? Reply with just the city." 30 '(^|[^A-Za-z])Paris($|[^A-Za-z])'
            run_test test_voice "Voice turn (message_id passthrough)" "$COOKIE_JAR" 45
        else
            info "LLM test skipped (--no-llm)"
        fi

        if [[ "$MODE" == "full" ]]; then
            section "Contacts CRUD"
            run_contacts_crud "$COOKIE_JAR"

            section "Email tool"
            run_email_tool_test "$COOKIE_JAR"
        else
            info "Full tests skipped (pass --full to enable CRUD/email/infra)"
        fi

    else
        fail "Service login (got $LOGIN_STATUS)"
    fi

    rm -f "$COOKIE_JAR"
else
    echo ""
    warn "SMOKE_TEST_SECRET not set - skipping authenticated + LLM tests"
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
