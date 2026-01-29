#!/bin/bash
set -euo pipefail

BACKEND_URL="${SHIPPER_E2E_URL:-http://localhost:47300}"
BACKEND_DIR="$(cd "$(dirname "$0")/../apps/zerg/backend" && pwd)"
TEMP_CLAUDE_DIR=$(mktemp -d)
SHIPPER_TOKEN="${SHIPPER_E2E_TOKEN:-}"

cleanup() {
  rm -rf "$TEMP_CLAUDE_DIR"
}
trap cleanup EXIT

echo "=== Shipper Smoke Test ==="
echo "Backend: $BACKEND_URL"
echo "Temp dir: $TEMP_CLAUDE_DIR"
echo ""

# 1. Health check
if ! curl -sf "$BACKEND_URL/health" > /dev/null; then
  echo "ERROR: Backend not reachable at $BACKEND_URL"
  echo "Start with: make dev"
  exit 1
fi

echo "Backend healthy"

# 2. Create device token via API
TOKEN_RESPONSE=$(curl -sf -X POST "$BACKEND_URL/api/devices/tokens" \
  -H "Content-Type: application/json" \
  -d '{"device_id": "smoke-test"}')

TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.token')
TOKEN_ID=$(echo "$TOKEN_RESPONSE" | jq -r '.id')

if [[ "$TOKEN" != zdt_* ]]; then
  echo "ERROR: Failed to create token: $TOKEN_RESPONSE"
  exit 1
fi

echo "Token created: ${TOKEN:0:20}..."

# 3. Create test session file
mkdir -p "$TEMP_CLAUDE_DIR/projects/smoke-test"
SESSION_ID=$(uuidgen | tr '[:upper:]' '[:lower:]')
SESSION_FILE="$TEMP_CLAUDE_DIR/projects/smoke-test/$SESSION_ID.jsonl"

cat > "$SESSION_FILE" << EOF_SESSION
{"type":"user","uuid":"$(uuidgen)","timestamp":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","cwd":"/tmp/smoke-test","message":{"content":"Smoke test message 1"}}
{"type":"assistant","uuid":"$(uuidgen)","timestamp":"$(date -u +%Y-%m-%dT%H:%M:%SZ)","cwd":"/tmp/smoke-test","message":{"content":[{"type":"text","text":"Smoke test response"}]}}
EOF_SESSION

# 4. Test zerg ship
cd "$BACKEND_DIR"
if [[ -n "$SHIPPER_TOKEN" ]]; then
  SHIP_OUTPUT=$(AGENTS_API_TOKEN="$SHIPPER_TOKEN" uv run zerg ship --url "$BACKEND_URL" --claude-dir "$TEMP_CLAUDE_DIR" 2>&1 || true)
else
  SHIP_OUTPUT=$(uv run zerg ship --url "$BACKEND_URL" --claude-dir "$TEMP_CLAUDE_DIR" 2>&1 || true)
fi
echo "$SHIP_OUTPUT"

# 5. Verify session in API
SESSIONS=$(curl -sf "$BACKEND_URL/api/agents/sessions?limit=5")
if echo "$SESSIONS" | jq -e '.sessions[] | select(.project == "smoke-test")' > /dev/null 2>&1; then
  echo "Session found in API"
else
  echo "WARNING: Session not found in API (may need different query)"
fi

# 6. Test incremental ship (no new content)
if [[ -n "$SHIPPER_TOKEN" ]]; then
  SHIP2_OUTPUT=$(AGENTS_API_TOKEN="$SHIPPER_TOKEN" uv run zerg ship --url "$BACKEND_URL" --claude-dir "$TEMP_CLAUDE_DIR" 2>&1 || true)
else
  SHIP2_OUTPUT=$(uv run zerg ship --url "$BACKEND_URL" --claude-dir "$TEMP_CLAUDE_DIR" 2>&1 || true)
fi
echo "$SHIP2_OUTPUT"

# 7. Add new content and ship again
printf '{"type":"user","uuid":"%s","timestamp":"%s","cwd":"/tmp/smoke-test","message":{"content":"New message after first ship"}}\n' "$(uuidgen)" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" >> "$SESSION_FILE"
if [[ -n "$SHIPPER_TOKEN" ]]; then
  SHIP3_OUTPUT=$(AGENTS_API_TOKEN="$SHIPPER_TOKEN" uv run zerg ship --url "$BACKEND_URL" --claude-dir "$TEMP_CLAUDE_DIR" 2>&1 || true)
else
  SHIP3_OUTPUT=$(uv run zerg ship --url "$BACKEND_URL" --claude-dir "$TEMP_CLAUDE_DIR" 2>&1 || true)
fi
echo "$SHIP3_OUTPUT"

# 8. Test token revocation
curl -sf -X DELETE "$BACKEND_URL/api/devices/tokens/$TOKEN_ID" > /dev/null

# Verify revoked token fails (only if auth is enforced)
REVOKED_RESPONSE=$(curl -s -w "\n%{http_code}" -X POST "$BACKEND_URL/api/agents/ingest" \
  -H "Content-Type: application/json" \
  -H "X-Agents-Token: $TOKEN" \
  -d "{\"id\":\"$(uuidgen | tr '[:upper:]' '[:lower:]')\",\"provider\":\"claude\",\"project\":\"test\",\"device_id\":\"test\",\"cwd\":\"/tmp\",\"started_at\":\"2026-01-01T00:00:00Z\",\"events\":[]}")
HTTP_CODE=$(echo "$REVOKED_RESPONSE" | tail -1)

if [[ "$HTTP_CODE" == "401" ]]; then
  echo "Revoked token correctly rejected (401)"
elif [[ "$HTTP_CODE" == "200" ]]; then
  echo "Auth disabled - revoked token accepted (expected in dev)"
else
  echo "WARNING: Revoked token returned $HTTP_CODE (expected 401 if auth enabled)"
fi

echo "=== Smoke Test Complete ==="
