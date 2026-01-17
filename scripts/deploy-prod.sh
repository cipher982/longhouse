#!/usr/bin/env bash
# Deploy to production via Coolify API
#
# Usage:
#   ./scripts/deploy-prod.sh           # Deploy and run smoke tests
#   ./scripts/deploy-prod.sh --skip-smoke  # Deploy only, skip smoke tests
#
# Requirements:
#   - SSH access to clifford (Coolify master)
#   - Coolify API token at /var/lib/docker/data/coolify-api/token.env on clifford

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_UUID="mosksc0ogk0cssokckw0c8sc"  # Swarmlet application UUID in Coolify
SKIP_SMOKE=false
POLL_INTERVAL=10
MAX_WAIT=300  # 5 minutes

# Parse args
for arg in "$@"; do
  case $arg in
    --skip-smoke)
      SKIP_SMOKE=true
      shift
      ;;
  esac
done

echo "=== Deploying Swarmlet to Production ==="
echo ""

# Sync user context and credentials to prod (before rebuild)
echo "Syncing user config to prod..."

LOCAL_CONFIG="apps/zerg/backend/scripts"
REMOTE_CONFIG="~/.config/zerg"

# Ensure remote dir exists
ssh zerg "mkdir -p $REMOTE_CONFIG"

# User context (required)
if [ ! -f "$LOCAL_CONFIG/user_context.local.json" ]; then
  echo "ERROR: $LOCAL_CONFIG/user_context.local.json not found"
  echo "Copy from user_context.example.json and customize"
  exit 1
fi
scp "$LOCAL_CONFIG/user_context.local.json" "zerg:$REMOTE_CONFIG/user_context.json"
echo "  ✓ User context synced"

# Credentials (required)
if [ ! -f "$LOCAL_CONFIG/personal_credentials.local.json" ]; then
  echo "ERROR: $LOCAL_CONFIG/personal_credentials.local.json not found"
  echo "Copy from personal_credentials.example.json and customize"
  exit 1
fi
scp "$LOCAL_CONFIG/personal_credentials.local.json" "zerg:$REMOTE_CONFIG/personal_credentials.json"
echo "  ✓ Personal credentials synced"

echo ""

# Get API token from clifford
echo "Fetching Coolify API token..."
TOKEN=$(ssh clifford "sudo cat /var/lib/docker/data/coolify-api/token.env 2>/dev/null | sed -n 's/^COOLIFY_API_TOKEN=//p'" 2>/dev/null || true)

if [[ -z "$TOKEN" ]]; then
  echo "ERROR: Could not fetch Coolify API token from clifford"
  echo "Make sure you have SSH access and sudo permissions"
  exit 1
fi

# Trigger deployment
echo "Triggering deployment (force rebuild)..."
RESPONSE=$(ssh clifford "curl -s -X POST 'http://localhost:8000/api/v1/deploy?uuid=${APP_UUID}&force=true' \
  -H 'Authorization: Bearer ${TOKEN}' \
  -H 'Content-Type: application/json'" 2>&1)

# Check if deploy was triggered successfully
if echo "$RESPONSE" | grep -q '"message"'; then
  MESSAGE=$(echo "$RESPONSE" | sed -n 's/.*"message"\s*:\s*"\([^"]*\)".*/\1/p' || echo "unknown")
  echo "Deploy response: $MESSAGE"
else
  echo "Deploy triggered. Response: $RESPONSE"
fi

echo ""
echo "Waiting for deployment to complete..."
echo "(Polling every ${POLL_INTERVAL}s, max ${MAX_WAIT}s)"
echo ""

# Poll for completion
ELAPSED=0
LAST_STATUS=""

while [[ $ELAPSED -lt $MAX_WAIT ]]; do
  # Get latest deployment status
  STATUS_LINE=$("${SCRIPT_DIR}/get-coolify-logs.sh" 1 2>/dev/null | grep -E "^status=" | head -1 || echo "status=unknown")
  CURRENT_STATUS=$(echo "$STATUS_LINE" | cut -d= -f2)

  if [[ "$CURRENT_STATUS" != "$LAST_STATUS" ]]; then
    echo "[${ELAPSED}s] Status: $CURRENT_STATUS"
    LAST_STATUS="$CURRENT_STATUS"
  fi

  case "$CURRENT_STATUS" in
    finished)
      echo ""
      echo "✅ Deployment completed successfully!"
      break
      ;;
    failed|cancelled)
      echo ""
      echo "❌ Deployment failed with status: $CURRENT_STATUS"
      echo ""
      echo "Run './scripts/get-coolify-logs.sh 1' for full logs"
      exit 1
      ;;
    *)
      # Still in progress (queued, in_progress, etc)
      sleep $POLL_INTERVAL
      ELAPSED=$((ELAPSED + POLL_INTERVAL))
      ;;
  esac
done

if [[ $ELAPSED -ge $MAX_WAIT ]]; then
  echo ""
  echo "⚠️  Timeout waiting for deployment (${MAX_WAIT}s)"
  echo "Check status manually: ./scripts/get-coolify-logs.sh 1"
  exit 1
fi

# Run smoke tests
if [[ "$SKIP_SMOKE" == "false" ]]; then
  echo ""
  echo "=== Running Smoke Tests ==="
  echo ""

  # Wait for backend health check to pass (has 60s start-period)
  echo "Waiting 60s for backend health checks..."
  sleep 60

  if "${SCRIPT_DIR}/smoke-prod.sh"; then
    echo ""
    echo "✅ All smoke tests passed!"
  else
    echo ""
    echo "❌ Smoke tests failed!"
    exit 1
  fi
else
  echo ""
  echo "Skipping smoke tests (--skip-smoke)"
fi

echo ""
echo "=== Deployment Complete ==="
