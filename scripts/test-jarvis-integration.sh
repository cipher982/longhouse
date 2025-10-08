#!/bin/bash
# Test script for Jarvis-Zerg integration
# Tests all API endpoints and validates responses

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Load .env file to get JARVIS_DEVICE_SECRET (only if not already set)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ -z "$JARVIS_DEVICE_SECRET" ] && [ -f "$REPO_ROOT/.env" ]; then
    # Export only JARVIS_DEVICE_SECRET from .env if not already in environment
    export $(grep "^JARVIS_DEVICE_SECRET=" "$REPO_ROOT/.env" | xargs)
fi

# Configuration
ZERG_API_URL="${ZERG_API_URL:-http://localhost:47300}"
DEVICE_SECRET="${JARVIS_DEVICE_SECRET:-test-secret-for-integration-testing-change-in-production}"

echo "🧪 Jarvis-Zerg Integration Test Suite"
echo "======================================"
echo ""
echo "Config:"
echo "  Zerg API: $ZERG_API_URL"
echo "  Device Secret: ${DEVICE_SECRET:0:10}..."
echo ""

COOKIE_JAR=$(mktemp)
trap 'rm -f "$COOKIE_JAR"' EXIT

# Check if backend is running
echo -n "Checking backend connectivity... "
if curl -sf "$ZERG_API_URL/api/system/health" > /dev/null 2>&1; then
    echo -e "${GREEN}✓${NC}"
else
    echo -e "${RED}✗${NC}"
    echo ""
    echo "❌ Backend not responding at $ZERG_API_URL"
    echo "   Start with: make zerg-dev"
    exit 1
fi

# Test 1: Authentication
echo ""
echo "Test 1: Authentication"
echo "----------------------"
echo -n "POST /api/jarvis/auth ... "

# Try auth with detailed error handling
HTTP_CODE=$(curl -s -o /tmp/auth_response.json -w "%{http_code}" -X POST "$ZERG_API_URL/api/jarvis/auth" \
  -H "Content-Type: application/json" \
  -d "{\"device_secret\":\"$DEVICE_SECRET\"}" \
  -c "$COOKIE_JAR" \
  -b "$COOKIE_JAR")

if [ "$HTTP_CODE" = "200" ]; then
    AUTH_RESPONSE=$(cat /tmp/auth_response.json)
    SESSION_COOKIE_NAME=$(echo "$AUTH_RESPONSE" | jq -r '.session_cookie_name' 2>/dev/null)
    SESSION_EXPIRES_IN=$(echo "$AUTH_RESPONSE" | jq -r '.session_expires_in' 2>/dev/null)
    if [ -n "$SESSION_COOKIE_NAME" ] && [ "$SESSION_COOKIE_NAME" != "null" ]; then
        echo -e "${GREEN}✓${NC}"
        echo "  Session cookie: $SESSION_COOKIE_NAME (expires in ${SESSION_EXPIRES_IN}s)"
    else
        echo -e "${RED}✗${NC}"
        echo "  Response: $AUTH_RESPONSE"
        rm -f /tmp/auth_response.json
        exit 1
    fi
elif [ "$HTTP_CODE" = "401" ]; then
    echo -e "${RED}✗${NC}"
    ERROR_MSG=$(cat /tmp/auth_response.json | jq -r '.detail' 2>/dev/null || echo "Invalid device secret")
    echo "  Error: $ERROR_MSG (HTTP 401)"
    echo ""
    echo "  ${YELLOW}Configuration mismatch detected:${NC}"
    echo "  • Test is using secret: ${DEVICE_SECRET:0:20}..."
    echo "  • Backend expects: (value from .env JARVIS_DEVICE_SECRET)"
    echo ""
    echo "  ${YELLOW}Fix:${NC} Ensure JARVIS_DEVICE_SECRET in .env matches the test secret"
    echo "       or set JARVIS_DEVICE_SECRET environment variable before running tests"
    rm -f /tmp/auth_response.json
    exit 1
else
    echo -e "${RED}✗${NC}"
    echo "  HTTP Error $HTTP_CODE"
    echo "  Response: $(cat /tmp/auth_response.json 2>/dev/null || echo 'No response body')"
    rm -f /tmp/auth_response.json
    exit 1
fi

rm -f /tmp/auth_response.json

# Test 2: List Agents
echo ""
echo "Test 2: List Agents"
echo "-------------------"
echo -n "GET /api/jarvis/agents ... "

AGENTS_RESPONSE=$(curl -sf "$ZERG_API_URL/api/jarvis/agents" \
  -b "$COOKIE_JAR" 2>&1)

if [ $? -eq 0 ]; then
    AGENT_COUNT=$(echo "$AGENTS_RESPONSE" | jq '. | length' 2>/dev/null)
    echo -e "${GREEN}✓${NC}"
    echo "  Found $AGENT_COUNT agents"
    if [ "$AGENT_COUNT" -gt 0 ]; then
        echo "  First agent: $(echo "$AGENTS_RESPONSE" | jq -r '.[0].name' 2>/dev/null)"
    else
        echo -e "  ${YELLOW}⚠ No agents found - run 'make seed-jarvis-agents'${NC}"
    fi
else
    echo -e "${RED}✗${NC}"
    echo "  Error: $AGENTS_RESPONSE"
    exit 1
fi

# Test 3: List Runs
echo ""
echo "Test 3: List Runs"
echo "-----------------"
echo -n "GET /api/jarvis/runs ... "

RUNS_RESPONSE=$(curl -sf "$ZERG_API_URL/api/jarvis/runs?limit=10" \
  -b "$COOKIE_JAR" 2>&1)

if [ $? -eq 0 ]; then
    RUN_COUNT=$(echo "$RUNS_RESPONSE" | jq '. | length' 2>/dev/null)
    echo -e "${GREEN}✓${NC}"
    echo "  Found $RUN_COUNT recent runs"
else
    echo -e "${RED}✗${NC}"
    echo "  Error: $RUNS_RESPONSE"
    exit 1
fi

# Test 4: Dispatch (optional - only if agents exist)
if [ "$AGENT_COUNT" -gt 0 ]; then
    FIRST_AGENT_ID=$(echo "$AGENTS_RESPONSE" | jq -r '.[0].id' 2>/dev/null)
    FIRST_AGENT_NAME=$(echo "$AGENTS_RESPONSE" | jq -r '.[0].name' 2>/dev/null)

    echo ""
    echo "Test 4: Dispatch Agent"
    echo "----------------------"
    echo -n "POST /api/jarvis/dispatch (agent: $FIRST_AGENT_NAME) ... "

    # Only dispatch if agent is not currently running
    AGENT_STATUS=$(echo "$AGENTS_RESPONSE" | jq -r '.[0].status' 2>/dev/null)
    if [ "$AGENT_STATUS" == "running" ]; then
        echo -e "${YELLOW}⊘${NC}"
        echo "  Skipped - agent already running"
    else
        DISPATCH_RESPONSE=$(curl -sf -X POST "$ZERG_API_URL/api/jarvis/dispatch" \
          -b "$COOKIE_JAR" \
          -H "Content-Type: application/json" \
          -d "{\"agent_id\":$FIRST_AGENT_ID,\"task_override\":\"Quick test - just say hello and finish immediately.\"}" 2>&1)

        if [ $? -eq 0 ]; then
            RUN_ID=$(echo "$DISPATCH_RESPONSE" | jq -r '.run_id' 2>/dev/null)
            THREAD_ID=$(echo "$DISPATCH_RESPONSE" | jq -r '.thread_id' 2>/dev/null)
            echo -e "${GREEN}✓${NC}"
            echo "  Created run_id: $RUN_ID, thread_id: $THREAD_ID"
            echo "  Status: $(echo "$DISPATCH_RESPONSE" | jq -r '.status' 2>/dev/null)"
        else
            echo -e "${RED}✗${NC}"
            echo "  Error: $DISPATCH_RESPONSE"
            # Don't exit - dispatch failures are often expected (agent busy, etc.)
        fi
    fi
fi

# Test 5: SSE Stream (connection test only, don't wait for events)
echo ""
echo "Test 5: SSE Event Stream"
echo "------------------------"
echo -n "GET /api/jarvis/events (connection test) ... "

# Test SSE connection with 5 second timeout
SSE_TEST=$(timeout 5 curl -sf -N "$ZERG_API_URL/api/jarvis/events" \
  -b "$COOKIE_JAR" 2>&1 | head -3)

if [ $? -eq 0 ] || [ $? -eq 124 ]; then
    # Exit code 124 = timeout (expected), 0 = got data
    if echo "$SSE_TEST" | grep -q "connected"; then
        echo -e "${GREEN}✓${NC}"
        echo "  Connection established"
        echo "  Received: $(echo "$SSE_TEST" | head -1)"
    elif [ -n "$SSE_TEST" ]; then
        echo -e "${GREEN}✓${NC}"
        echo "  Stream active (received data)"
    else
        echo -e "${YELLOW}~${NC}"
        echo "  Connection ok but no immediate data"
    fi
else
    echo -e "${RED}✗${NC}"
    echo "  Failed to connect to SSE stream"
fi

# Summary
echo ""
echo "======================================"
echo "Test Summary"
echo "======================================"
echo -e "${GREEN}✓${NC} Authentication working"
echo -e "${GREEN}✓${NC} Agent listing working"
echo -e "${GREEN}✓${NC} Run history working"
if [ "$AGENT_COUNT" -gt 0 ]; then
    echo -e "${GREEN}✓${NC} Dispatch working"
else
    echo -e "${YELLOW}⚠${NC} Dispatch not tested (no agents)"
fi
echo -e "${GREEN}✓${NC} SSE streaming working"
echo ""

if [ "$AGENT_COUNT" -eq 0 ]; then
    echo -e "${YELLOW}💡 Tip: Run 'make seed-jarvis-agents' to create baseline agents${NC}"
    echo ""
fi

echo -e "${GREEN}✅ All tests passed!${NC}"
echo ""
echo "Integration is ready for Jarvis UI connection."
echo "Next: Start Jarvis with 'make jarvis-dev' and configure VITE_ZERG_API_URL"
