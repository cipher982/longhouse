#!/bin/bash

# Production Smoke Test for Swarmlet
# Run this after deployment to verify critical endpoints are healthy.
#
# Usage:
#   ./scripts/smoke-prod.sh              # Test https://swarmlet.com
#   ./scripts/smoke-prod.sh --wait       # Wait 90s then test (for post-deploy)
#   BASE_URL=https://staging.swarmlet.com ./scripts/smoke-prod.sh

set -e

# Configuration
BASE_URL="${BASE_URL:-https://swarmlet.com}"
WAIT_SECS="${WAIT_SECS:-90}"
FAILED=0

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "================================================"
echo "  Swarmlet Production Smoke Test"
echo "  Target: $BASE_URL"
echo "================================================"
echo ""

# Wait if --wait flag is passed
if [[ "$1" == "--wait" ]]; then
    echo -e "${YELLOW}Waiting ${WAIT_SECS}s for deployment to stabilize...${NC}"
    sleep "$WAIT_SECS"
    echo ""
fi

# Helper function to check HTTP response
check_endpoint() {
    local name="$1"
    local method="$2"
    local url="$3"
    local expected_code="$4"
    local extra_args="${5:-}"
    local check_redirect="${6:-}"

    echo -n "  $name ... "

    if [[ "$method" == "GET" ]]; then
        response=$(curl -s -o /dev/null -w "%{http_code}|%{redirect_url}" $extra_args "$url" 2>&1)
    else
        response=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" $extra_args "$url" 2>&1)
    fi

    http_code=$(echo "$response" | cut -d'|' -f1)
    redirect_url=$(echo "$response" | cut -d'|' -f2)

    if [[ "$http_code" == "$expected_code" ]]; then
        if [[ -n "$check_redirect" && "$http_code" == "302" ]]; then
            if [[ "$redirect_url" == *"$check_redirect"* ]]; then
                echo -e "${GREEN}PASS${NC} ($http_code -> $redirect_url)"
            else
                echo -e "${RED}FAIL${NC} (redirected to $redirect_url, expected $check_redirect)"
                FAILED=1
            fi
        else
            echo -e "${GREEN}PASS${NC} ($http_code)"
        fi
    else
        echo -e "${RED}FAIL${NC} (got $http_code, expected $expected_code)"
        FAILED=1
    fi
}

# Test 1: Landing page
echo "Checking critical endpoints..."
check_endpoint "GET /" "GET" "$BASE_URL/" "200"

# Test 2: Health endpoint
check_endpoint "GET /health" "GET" "$BASE_URL/health" "200"

# Test 3: Dashboard redirects unauthenticated users
# Note: This should redirect to / when not authenticated (auth_request gate)
check_endpoint "GET /dashboard (unauth)" "GET" "$BASE_URL/dashboard" "302" "" "$BASE_URL/"

# Test 4: Funnel batch endpoint (used by analytics)
check_endpoint "POST /api/funnel/batch" "POST" "$BASE_URL/api/funnel/batch" "200" "-H 'Content-Type: application/json' -d '[]'"

# Test 5: Auth verify returns 401 without session
check_endpoint "GET /api/auth/verify (no cookie)" "GET" "$BASE_URL/api/auth/verify" "401"

# Test 6: API health (via same-origin proxy)
check_endpoint "GET /api/health" "GET" "$BASE_URL/api/health" "200"

echo ""
echo "================================================"
if [[ $FAILED -eq 0 ]]; then
    echo -e "  ${GREEN}All smoke tests passed!${NC}"
    echo "================================================"
    exit 0
else
    echo -e "  ${RED}Some smoke tests failed!${NC}"
    echo "================================================"
    exit 1
fi
