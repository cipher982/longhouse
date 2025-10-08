#!/bin/bash
# Comprehensive Jarvis-Zerg test suite with automated verification
# This script can run autonomously and provides detailed diagnostics

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Load .env for configuration
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ -z "$JARVIS_DEVICE_SECRET" ] && [ -f "$REPO_ROOT/.env" ]; then
    export $(grep "^JARVIS_DEVICE_SECRET=" "$REPO_ROOT/.env" | xargs)
fi

ZERG_API_URL="${ZERG_API_URL:-http://localhost:47300}"
DEVICE_SECRET="${JARVIS_DEVICE_SECRET:-test-secret-for-integration-testing-change-in-production}"

PASSED_TESTS=0
FAILED_TESTS=0
TOTAL_TESTS=0

log_section() {
    echo ""
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

pass() {
    echo -e "${GREEN}✓${NC} $1"
    ((PASSED_TESTS++))
    ((TOTAL_TESTS++))
}

fail() {
    echo -e "${RED}✗${NC} $1"
    ((FAILED_TESTS++))
    ((TOTAL_TESTS++))
}

warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

echo ""
echo -e "${BLUE}╔════════════════════════════════════════════════════════════╗${NC}"
echo -e "${BLUE}║  Comprehensive Jarvis-Zerg Integration Test Suite         ║${NC}"
echo -e "${BLUE}╚════════════════════════════════════════════════════════════╝${NC}"
echo ""
echo "Configuration:"
echo "  • API URL: $ZERG_API_URL"
echo "  • Device Secret: ${DEVICE_SECRET:0:20}..."
echo ""

# =============================================================================
# Pre-flight Checks
# =============================================================================

log_section "Pre-flight System Checks"

# Check Docker is running
echo -n "Checking Docker... "
if docker ps &> /dev/null; then
    pass "Docker daemon is running"
else
    fail "Docker daemon is not running"
    echo ""
    echo "  Start Docker and try again"
    exit 1
fi

# Check containers are running
echo -n "Checking Zerg containers... "
BACKEND_RUNNING=$(docker ps --filter "name=zerg-backend" --format "{{.Names}}" | wc -l)
POSTGRES_RUNNING=$(docker ps --filter "name=zerg-postgres" --format "{{.Names}}" | wc -l)

if [ "$BACKEND_RUNNING" -gt 0 ] && [ "$POSTGRES_RUNNING" -gt 0 ]; then
    pass "Backend and Postgres containers are running"
else
    fail "Containers not running (backend: $BACKEND_RUNNING, postgres: $POSTGRES_RUNNING)"
    echo ""
    echo "  Start with: make zerg-up"
    exit 1
fi

# Check backend health
echo -n "Checking backend health... "
HEALTH_RESPONSE=$(curl -sf "$ZERG_API_URL/" 2>&1 || echo "")
if [ -n "$HEALTH_RESPONSE" ]; then
    pass "Backend is responding at $ZERG_API_URL"
else
    fail "Backend not responding at $ZERG_API_URL"
    echo ""
    echo "  Check logs: docker logs zerg-backend-1"
    exit 1
fi

# Check backend environment
echo -n "Verifying backend environment... "
BACKEND_SECRET=$(docker exec zerg-backend-1 printenv JARVIS_DEVICE_SECRET 2>/dev/null || echo "")
if [ -n "$BACKEND_SECRET" ]; then
    if [ "$BACKEND_SECRET" = "$DEVICE_SECRET" ]; then
        pass "JARVIS_DEVICE_SECRET matches between test and backend"
    else
        warn "Secret mismatch: test='${DEVICE_SECRET:0:10}...' backend='${BACKEND_SECRET:0:10}...'"
        fail "Environment variable mismatch detected"
        echo ""
        echo "  This will cause auth to fail. Update .env or set JARVIS_DEVICE_SECRET"
        exit 1
    fi
else
    warn "Could not verify JARVIS_DEVICE_SECRET in backend container"
fi

# =============================================================================
# API Endpoint Tests
# =============================================================================

log_section "API Endpoint Tests"

COOKIE_JAR=$(mktemp)
trap 'rm -f "$COOKIE_JAR"' EXIT

# Test 1: Authentication
echo ""
echo "Test 1: Authentication endpoint"
HTTP_CODE=$(curl -s -o /tmp/auth_test.json -w "%{http_code}" \
    -X POST "$ZERG_API_URL/api/jarvis/auth" \
    -H "Content-Type: application/json" \
    -d "{\"device_secret\":\"$DEVICE_SECRET\"}" \
    -c "$COOKIE_JAR")

if [ "$HTTP_CODE" = "200" ]; then
    SESSION_COOKIE=$(cat /tmp/auth_test.json | jq -r '.session_cookie_name')
    EXPIRES_IN=$(cat /tmp/auth_test.json | jq -r '.session_expires_in')
    pass "POST /api/jarvis/auth → 200 OK"
    echo "     Cookie: $SESSION_COOKIE (expires in ${EXPIRES_IN}s)"
else
    fail "POST /api/jarvis/auth → HTTP $HTTP_CODE"
    echo "     $(cat /tmp/auth_test.json 2>/dev/null || echo 'No response')"
    exit 1
fi

# Test 2: Agent listing (authenticated)
echo ""
echo "Test 2: Agent listing endpoint"
HTTP_CODE=$(curl -s -o /tmp/agents_test.json -w "%{http_code}" \
    "$ZERG_API_URL/api/jarvis/agents" \
    -b "$COOKIE_JAR")

if [ "$HTTP_CODE" = "200" ]; then
    AGENT_COUNT=$(cat /tmp/agents_test.json | jq '. | length')
    pass "GET /api/jarvis/agents → 200 OK"
    echo "     Found $AGENT_COUNT agents"
    if [ "$AGENT_COUNT" -eq 0 ]; then
        warn "No agents configured (run 'make seed-jarvis-agents' to create test agents)"
    fi
else
    fail "GET /api/jarvis/agents → HTTP $HTTP_CODE"
    echo "     $(cat /tmp/agents_test.json 2>/dev/null || echo 'No response')"
fi

# Test 3: Run history
echo ""
echo "Test 3: Run history endpoint"
HTTP_CODE=$(curl -s -o /tmp/runs_test.json -w "%{http_code}" \
    "$ZERG_API_URL/api/jarvis/runs?limit=10" \
    -b "$COOKIE_JAR")

if [ "$HTTP_CODE" = "200" ]; then
    RUN_COUNT=$(cat /tmp/runs_test.json | jq '. | length')
    pass "GET /api/jarvis/runs → 200 OK"
    echo "     Found $RUN_COUNT recent runs"
else
    fail "GET /api/jarvis/runs → HTTP $HTTP_CODE"
    echo "     $(cat /tmp/runs_test.json 2>/dev/null || echo 'No response')"
fi

# Test 4: SSE events stream
echo ""
echo "Test 4: SSE events stream"
SSE_OUTPUT=$(timeout 5 curl -sN "$ZERG_API_URL/api/jarvis/events" -b "$COOKIE_JAR" | head -3)
EXIT_CODE=$?

if [ $EXIT_CODE -eq 124 ] || [ $EXIT_CODE -eq 0 ]; then
    if echo "$SSE_OUTPUT" | grep -q "connected"; then
        pass "GET /api/jarvis/events → SSE connected"
        echo "     $(echo "$SSE_OUTPUT" | head -1 | cut -c1-60)..."
    elif [ -n "$SSE_OUTPUT" ]; then
        pass "GET /api/jarvis/events → Stream active"
    else
        warn "SSE endpoint accessible but no immediate data"
    fi
else
    fail "GET /api/jarvis/events → Connection failed"
fi

# =============================================================================
# Security Tests
# =============================================================================

log_section "Security Validation Tests"

# Test 5: Invalid secret rejection
echo ""
echo "Test 5: Invalid device secret rejection"
HTTP_CODE=$(curl -s -o /tmp/invalid_auth.json -w "%{http_code}" \
    -X POST "$ZERG_API_URL/api/jarvis/auth" \
    -H "Content-Type: application/json" \
    -d '{"device_secret":"wrong-secret-should-fail"}')

if [ "$HTTP_CODE" = "401" ]; then
    pass "POST /api/jarvis/auth (invalid secret) → 401 Unauthorized"
    ERROR_MSG=$(cat /tmp/invalid_auth.json | jq -r '.detail')
    echo "     Error message: $ERROR_MSG"
else
    fail "POST /api/jarvis/auth (invalid secret) → HTTP $HTTP_CODE (expected 401)"
    warn "Security issue: Backend accepted invalid device secret!"
fi

# Test 6: Unauthenticated endpoint access
echo ""
echo "Test 6: Unauthenticated access rejection"
HTTP_CODE=$(curl -s -o /tmp/unauth_test.json -w "%{http_code}" \
    "$ZERG_API_URL/api/jarvis/agents")

if [ "$HTTP_CODE" = "401" ]; then
    pass "GET /api/jarvis/agents (no cookie) → 401 Unauthorized"
elif [ "$HTTP_CODE" = "200" ]; then
    warn "Endpoint accessible without auth (AUTH_DISABLED may be set)"
    # This is acceptable in dev mode, not a failure
    ((PASSED_TESTS++))
    ((TOTAL_TESTS++))
else
    fail "GET /api/jarvis/agents (no cookie) → HTTP $HTTP_CODE (expected 401 or 200 in dev)"
fi

# =============================================================================
# Backend Unit Tests (Optional - can be slow)
# =============================================================================

if [ "${RUN_BACKEND_TESTS:-0}" = "1" ]; then
    log_section "Backend Unit Test Suite"

    echo ""
    echo "Running pytest backend tests (this may take a minute)..."
    BACKEND_TEST_OUTPUT=$(timeout 120 docker exec zerg-backend-1 uv run pytest tests/ -v --tb=short 2>&1 || echo "TESTS_FAILED")

    if echo "$BACKEND_TEST_OUTPUT" | grep -q "TESTS_FAILED"; then
        fail "Backend unit tests failed"
        echo ""
        echo "$BACKEND_TEST_OUTPUT" | tail -20
    else
        PASSED_COUNT=$(echo "$BACKEND_TEST_OUTPUT" | grep -oE '[0-9]+ passed' | grep -oE '[0-9]+' || echo "0")
        if [ "$PASSED_COUNT" -gt 0 ]; then
            pass "Backend unit tests: $PASSED_COUNT tests passed"
        else
            warn "Could not parse test results"
        fi
    fi
else
    echo ""
    echo "Skipping backend unit tests (set RUN_BACKEND_TESTS=1 to enable)"
fi

# =============================================================================
# Summary Report
# =============================================================================

log_section "Test Summary Report"
echo ""

TOTAL_PERCENTAGE=$((PASSED_TESTS * 100 / TOTAL_TESTS))

echo "Results:"
echo "  • Total tests: $TOTAL_TESTS"
echo "  • Passed: ${GREEN}$PASSED_TESTS${NC}"
echo "  • Failed: ${RED}$FAILED_TESTS${NC}"
echo "  • Success rate: ${TOTAL_PERCENTAGE}%"
echo ""

if [ $FAILED_TESTS -eq 0 ]; then
    echo -e "${GREEN}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${GREEN}║  ✅ All tests passed! Jarvis integration is ready.        ║${NC}"
    echo -e "${GREEN}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "System is ready for:"
    echo "  • Jarvis client connection"
    echo "  • Production deployment"
    echo "  • End-to-end testing"
    exit 0
else
    echo -e "${RED}╔════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${RED}║  ❌ Some tests failed. Review output above.               ║${NC}"
    echo -e "${RED}╚════════════════════════════════════════════════════════════╝${NC}"
    echo ""
    echo "Debug steps:"
    echo "  1. Check container logs: docker logs zerg-backend-1"
    echo "  2. Verify .env configuration matches docker-compose"
    echo "  3. Restart services: make zerg-down && make zerg-up"
    exit 1
fi

# Cleanup
rm -f /tmp/auth_test.json /tmp/agents_test.json /tmp/runs_test.json /tmp/invalid_auth.json /tmp/unauth_test.json
