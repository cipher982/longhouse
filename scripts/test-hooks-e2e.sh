#!/bin/bash
# E2E test for the Longhouse hook outbox pipeline.
#
# Validates the full chain:
#   hook rename pattern → ~/.claude/outbox/prs.*.json
#   → daemon drains (1s poll)
#   → POST /api/agents/presence
#   → file deleted
#   → API responds healthy
#
# Usage:
#   scripts/test-hooks-e2e.sh
#   make test-hooks
#
# Requirements:
#   - longhouse-engine daemon running (launchd: com.longhouse.shipper)
#   - ~/.claude/longhouse-url and ~/.claude/longhouse-device-token present
#   - jq, curl

set -euo pipefail

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
OUTBOX="$CLAUDE_DIR/outbox"
URL_FILE="$CLAUDE_DIR/longhouse-url"
TOKEN_FILE="$CLAUDE_DIR/longhouse-device-token"

PASS_COUNT=0
FAIL_COUNT=0

pass() { echo "  PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "  FAIL: $1" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }
section() { echo ""; echo "=== $1 ==="; }

# ---------------------------------------------------------------------------
section "Prerequisites"
# ---------------------------------------------------------------------------

command -v jq  >/dev/null 2>&1 && pass "jq available"      || { fail "jq not installed"; exit 1; }
command -v curl >/dev/null 2>&1 && pass "curl available"    || { fail "curl not installed"; exit 1; }

# Use grep -c to avoid SIGPIPE with pipefail (grep -q exits early, breaking the pipe)
DAEMON_RUNNING=$(launchctl list 2>/dev/null | grep -c "com.longhouse.shipper" || true)
[ "$DAEMON_RUNNING" -gt 0 ] \
  && pass "daemon running (com.longhouse.shipper)" \
  || { fail "daemon not running — start with: longhouse connect"; exit 1; }

[ -f "$URL_FILE" ]   && pass "longhouse-url present"   || { fail "missing $URL_FILE"; exit 1; }
[ -f "$TOKEN_FILE" ] && pass "longhouse-device-token present" || { fail "missing $TOKEN_FILE"; exit 1; }

API_URL="$(tr -d '[:space:]' < "$URL_FILE")"
TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"

[ -n "$API_URL" ]  && pass "API URL: $API_URL" || { fail "longhouse-url is empty"; exit 1; }
[ -n "$TOKEN" ]    && pass "device token present" || { fail "longhouse-device-token is empty"; exit 1; }

# ---------------------------------------------------------------------------
section "API health"
# ---------------------------------------------------------------------------

if curl -sf "$API_URL/api/health" >/dev/null; then
  pass "API reachable"
else
  fail "API unreachable at $API_URL/api/health"
  exit 1
fi

# ---------------------------------------------------------------------------
section "Outbox drain — correct prs.* filename pattern"
# ---------------------------------------------------------------------------

# Use an existing session_id if possible so presence row is queryable
SESSION_ID="$(
  curl -sf -H "X-Agents-Token: $TOKEN" \
    "$API_URL/api/agents/sessions?limit=1&days_back=1" \
  | jq -r '.sessions[0].id // empty' 2>/dev/null
)" || SESSION_ID=""

if [ -z "$SESSION_ID" ]; then
  SESSION_ID="hook-e2e-$(date +%s)"
  echo "  (no recent session found — using synthetic id: $SESSION_ID)"
fi

mkdir -p "$OUTBOX"

# Write using the exact hook rename: .tmp.XXXXXX → prs.XXXXXX.json
SUFFIX="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c 8 || date +%s%N)"
TMPFILE="$OUTBOX/.tmp.$SUFFIX"
FINALFILE="$OUTBOX/prs.$SUFFIX.json"

jq -n \
  --arg sid "$SESSION_ID" \
  --arg st  "thinking" \
  --arg tool "test-hooks-e2e" \
  --arg cwd  "$PWD" \
  '{session_id: $sid, state: $st, tool_name: $tool, cwd: $cwd}' > "$TMPFILE"
mv "$TMPFILE" "$FINALFILE"

echo "  wrote: $(basename "$FINALFILE")"

# Wait for daemon to drain (up to 10s, check every 0.5s)
DRAINED=0
for i in $(seq 1 20); do
  if [ ! -f "$FINALFILE" ]; then
    DRAINED=1
    break
  fi
  sleep 0.5
done

if [ "$DRAINED" -eq 1 ]; then
  pass "outbox file drained within $((i / 2))s"
else
  # Clean up and fail
  rm -f "$FINALFILE"
  fail "outbox file not drained after 10s — daemon may not be posting to API"
fi

# Verify presence endpoint is reachable and accepts events (not just file-gone).
# File deletion alone is a false positive — post_json used to return Ok() on any
# HTTP status. This direct curl confirms the full auth+API path works.
HTTP_STATUS="$(curl -s -o /dev/null -w "%{http_code}" -X POST \
  -H "X-Agents-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"session_id\":\"$SESSION_ID\",\"state\":\"thinking\",\"tool_name\":\"\",\"cwd\":\"$PWD\"}" \
  "$API_URL/api/agents/presence" 2>/dev/null)"

if [ "$HTTP_STATUS" = "204" ]; then
  pass "presence endpoint reachable and authenticated (HTTP 204)"
else
  fail "presence endpoint returned HTTP $HTTP_STATUS — expected 204 (auth or routing issue)"
fi

# ---------------------------------------------------------------------------
section "Outbox drain — dot-prefixed files must NOT be consumed"
# ---------------------------------------------------------------------------

DOT_SUFFIX="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom 2>/dev/null | head -c 8 || date +%s)"
DOT_FILE="$OUTBOX/.tmp.$DOT_SUFFIX.json"  # old bad pattern — must be skipped

jq -n \
  --arg sid "hook-e2e-dot-test" \
  --arg st  "thinking" \
  '{session_id: $sid, state: $st, tool_name: "", cwd: "/tmp"}' > "$DOT_FILE"

sleep 2  # give daemon a couple of ticks

if [ -f "$DOT_FILE" ]; then
  pass "dot-prefixed .tmp.*.json correctly ignored by daemon"
  rm -f "$DOT_FILE"
else
  fail "dot-prefixed file was consumed (drain filter regression)"
fi

# ---------------------------------------------------------------------------
section "Summary"
# ---------------------------------------------------------------------------

echo ""
TOTAL=$((PASS_COUNT + FAIL_COUNT))
echo "$PASS_COUNT/$TOTAL checks passed"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo "FAILED"
  exit 1
else
  echo "OK"
fi
