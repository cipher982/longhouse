#!/bin/bash
set -e

# Integration test script for runner daemon
# Tests the full flow: enroll -> register -> connect -> verify online

echo "======================================"
echo "Runner Integration Test"
echo "======================================"
echo ""

# Configuration
API_URL=${API_URL:-"http://localhost:47300"}
AUTH_HEADER="Authorization: Bearer test-token-user1"

# Check if backend is running
if ! curl -s "${API_URL}/health" > /dev/null 2>&1; then
    echo "❌ Backend not running at ${API_URL}"
    echo "Please start the backend with: make zerg"
    exit 1
fi

echo "✓ Backend is running"

# Step 1: Create enrollment token
echo ""
echo "Step 1: Creating enrollment token..."
ENROLL_RESPONSE=$(curl -s -X POST "${API_URL}/api/runners/enroll-token" \
    -H "${AUTH_HEADER}" \
    -H "Content-Type: application/json")

ENROLL_TOKEN=$(echo "${ENROLL_RESPONSE}" | jq -r '.enroll_token')
SWARMLET_URL=$(echo "${ENROLL_RESPONSE}" | jq -r '.swarmlet_url')

if [ "${ENROLL_TOKEN}" = "null" ] || [ -z "${ENROLL_TOKEN}" ]; then
    echo "❌ Failed to create enrollment token"
    echo "Response: ${ENROLL_RESPONSE}"
    exit 1
fi

echo "✓ Created enrollment token: ${ENROLL_TOKEN:0:20}..."

# Step 2: Register runner
echo ""
echo "Step 2: Registering runner..."
REGISTER_RESPONSE=$(curl -s -X POST "${API_URL}/api/runners/register" \
    -H "Content-Type: application/json" \
    -d "{
        \"enroll_token\": \"${ENROLL_TOKEN}\",
        \"name\": \"test-runner-$(date +%s)\",
        \"metadata\": {
            \"hostname\": \"test-host\",
            \"platform\": \"darwin\",
            \"arch\": \"arm64\"
        }
    }")

RUNNER_ID=$(echo "${REGISTER_RESPONSE}" | jq -r '.runner_id')
RUNNER_SECRET=$(echo "${REGISTER_RESPONSE}" | jq -r '.runner_secret')
RUNNER_NAME=$(echo "${REGISTER_RESPONSE}" | jq -r '.name')

if [ "${RUNNER_ID}" = "null" ] || [ -z "${RUNNER_ID}" ]; then
    echo "❌ Failed to register runner"
    echo "Response: ${REGISTER_RESPONSE}"
    exit 1
fi

echo "✓ Registered runner:"
echo "  ID: ${RUNNER_ID}"
echo "  Name: ${RUNNER_NAME}"
echo "  Secret: ${RUNNER_SECRET:0:20}..."

# Step 3: Start runner daemon in background
echo ""
echo "Step 3: Starting runner daemon..."

# Convert HTTP URL to WS URL for runner
WS_URL=$(echo "${SWARMLET_URL}" | sed 's/http:/ws:/')

RUNNER_PID_FILE="/tmp/runner-test-${RUNNER_ID}.pid"
RUNNER_LOG_FILE="/tmp/runner-test-${RUNNER_ID}.log"

# Start runner in background
SWARMLET_URL="${WS_URL}" \
RUNNER_ID="${RUNNER_ID}" \
RUNNER_SECRET="${RUNNER_SECRET}" \
bun run src/index.ts > "${RUNNER_LOG_FILE}" 2>&1 &

RUNNER_PID=$!
echo "${RUNNER_PID}" > "${RUNNER_PID_FILE}"

echo "✓ Runner daemon started (PID: ${RUNNER_PID})"
echo "  Log file: ${RUNNER_LOG_FILE}"

# Wait for connection
echo ""
echo "Step 4: Waiting for runner to connect..."
sleep 3

# Step 5: Verify runner is online
echo ""
echo "Step 5: Verifying runner status..."
RUNNER_STATUS=$(curl -s "${API_URL}/api/runners/${RUNNER_ID}" \
    -H "${AUTH_HEADER}")

STATUS=$(echo "${RUNNER_STATUS}" | jq -r '.status')
LAST_SEEN=$(echo "${RUNNER_STATUS}" | jq -r '.last_seen_at')

if [ "${STATUS}" != "online" ]; then
    echo "❌ Runner is not online (status: ${STATUS})"
    echo "Runner log:"
    cat "${RUNNER_LOG_FILE}"

    # Cleanup
    kill "${RUNNER_PID}" 2>/dev/null || true
    rm -f "${RUNNER_PID_FILE}" "${RUNNER_LOG_FILE}"
    exit 1
fi

echo "✓ Runner is online!"
echo "  Status: ${STATUS}"
echo "  Last seen: ${LAST_SEEN}"

# Step 6: Stop runner and verify offline
echo ""
echo "Step 6: Stopping runner and verifying offline status..."
kill "${RUNNER_PID}"
sleep 2

RUNNER_STATUS=$(curl -s "${API_URL}/api/runners/${RUNNER_ID}" \
    -H "${AUTH_HEADER}")

STATUS=$(echo "${RUNNER_STATUS}" | jq -r '.status')

if [ "${STATUS}" != "offline" ]; then
    echo "❌ Runner is not offline (status: ${STATUS})"
    exit 1
fi

echo "✓ Runner is offline"

# Cleanup
rm -f "${RUNNER_PID_FILE}" "${RUNNER_LOG_FILE}"

echo ""
echo "======================================"
echo "✓ All tests passed!"
echo "======================================"
