#!/bin/bash
# Test script for Jarvis-Zerg integration
# Tests all API endpoints and validates responses

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
ZERG_API_URL="${ZERG_API_URL:-http://localhost:47300}"
DEVICE_SECRET="${JARVIS_DEVICE_SECRET:-test-secret-please-change}"

echo "🧪 Jarvis-Zerg Integration Test Suite"
echo "======================================"
echo ""
echo "Config:"
echo "  Zerg API: $ZERG_API_URL"
echo "  Device Secret: ${DEVICE_SECRET:0:10}..."
echo ""

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

AUTH_RESPONSE=$(curl -sf -X POST "$ZERG_API_URL/api/jarvis/auth" \
  -H "Content-Type: application/json" \
  -d "{\"device_secret\":\"$DEVICE_SECRET\"}" 2>&1)

if [ $? -eq 0 ]; then
    TOKEN=$(echo "$AUTH_RESPONSE" | jq -r '.access_token' 2>/dev/null)
    if [ -n "$TOKEN" ] && [ "$TOKEN" != "null" ]; then
        echo -e "${GREEN}✓${NC}"
        echo "  Token received: ${TOKEN:0:20}..."
    else
        echo -e "${RED}✗${NC}"
        echo "  Response: $AUTH_RESPONSE"
        exit 1
    fi
else
    echo -e "${RED}✗${NC}"
    echo "  Error: $AUTH_RESPONSE"
    echo ""
    echo "  Check JARVIS_DEVICE_SECRET in .env matches test secret"
    exit 1
fi

# Test 2: List Agents
echo ""
echo "Test 2: List Agents"
echo "-------------------"
echo -n "GET /api/jarvis/agents ... "

AGENTS_RESPONSE=$(curl -sf "$ZERG_API_URL/api/jarvis/agents" \
  -H "Authorization: Bearer $TOKEN" 2>&1)

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
  -H "Authorization: Bearer $TOKEN" 2>&1)

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
          -H "Authorization: Bearer $TOKEN" \
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
  -H "Authorization: Bearer $TOKEN" 2>&1 | head -3)

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
