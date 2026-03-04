#!/bin/bash
# Probe script — simulate Claude Code hook payloads and verify outbox output.
# Run locally to validate that the hook script maps events → states correctly.
#
# Usage: bash scripts/probe-hook-payloads.sh
#
# Tests each known payload scenario:
#   - Existing events (UserPromptSubmit, PreToolUse, PostToolUse, Stop)
#   - New: PermissionRequest        → blocked
#   - New: Notification/idle_prompt → needs_user
#   - New: Notification/elicitation → needs_user
#   - New: Notification/permission_prompt → blocked
#   - Edge: Notification/auth_success → ignored
#   - Edge: Unknown event → ignored

set -euo pipefail

HOOK="${1:-$HOME/.claude/hooks/longhouse-hook.sh}"
OUTBOX="$HOME/.claude/outbox"
SESSION_ID="probe-test-$(date +%s)"
PASS=0
FAIL=0

# Ensure jq available
command -v jq >/dev/null 2>&1 || { echo "jq required"; exit 1; }

# Ensure hook exists
if [[ ! -f "$HOOK" ]]; then
  echo "ERROR: Hook script not found at $HOOK"
  echo "Run 'longhouse connect --install' first, or pass path as arg: $0 /path/to/longhouse-hook.sh"
  exit 1
fi

run_case() {
  local label="$1"
  local payload="$2"
  local expected_state="$3"   # "" means expect no outbox file written

  # Clean up any leftover prs.* files from this session
  find "$OUTBOX" -name "prs.*.json" -newer /tmp/probe-hook-start 2>/dev/null | xargs rm -f 2>/dev/null || true

  # Run the hook
  printf '%s' "$payload" | bash "$HOOK" 2>/dev/null

  # Find newest prs.* file written after our marker
  local written
  written=$(find "$OUTBOX" -name "prs.*.json" -newer /tmp/probe-hook-start 2>/dev/null | sort | tail -1)

  if [[ -z "$expected_state" ]]; then
    # Expect no file written (event should be ignored)
    if [[ -z "$written" ]]; then
      echo "PASS  $label  (correctly ignored)"
      ((PASS++)) || true
    else
      local got_state
      got_state=$(jq -r '.state' "$written" 2>/dev/null || echo "?")
      echo "FAIL  $label  (expected no output, got state=$got_state)"
      ((FAIL++)) || true
      rm -f "$written"
    fi
  else
    if [[ -z "$written" ]]; then
      echo "FAIL  $label  (expected state=$expected_state, got no output)"
      ((FAIL++)) || true
    else
      local got_state
      got_state=$(jq -r '.state' "$written" 2>/dev/null || echo "?")
      rm -f "$written"
      if [[ "$got_state" == "$expected_state" ]]; then
        echo "PASS  $label  (state=$got_state)"
        ((PASS++)) || true
      else
        echo "FAIL  $label  (expected state=$expected_state, got state=$got_state)"
        ((FAIL++)) || true
      fi
    fi
  fi
}

# Create timestamp marker for find -newer
touch /tmp/probe-hook-start

mkdir -p "$OUTBOX"

echo ""
echo "Probing hook: $HOOK"
echo "Outbox:       $OUTBOX"
echo "Session:      $SESSION_ID"
echo "─────────────────────────────────────────────────────"
echo ""

# ── Existing events ──────────────────────────────────────────────────────────

run_case "UserPromptSubmit → thinking" \
  "$(jq -n --arg sid "$SESSION_ID" '{
    hook_event_name: "UserPromptSubmit",
    session_id: $sid,
    cwd: "/tmp/test"
  }')" \
  "thinking"

run_case "PreToolUse → running" \
  "$(jq -n --arg sid "$SESSION_ID" '{
    hook_event_name: "PreToolUse",
    session_id: $sid,
    tool_name: "Bash",
    cwd: "/tmp/test"
  }')" \
  "running"

run_case "PostToolUse → thinking" \
  "$(jq -n --arg sid "$SESSION_ID" '{
    hook_event_name: "PostToolUse",
    session_id: $sid,
    tool_name: "Bash",
    cwd: "/tmp/test"
  }')" \
  "thinking"

run_case "PostToolUseFailure → thinking" \
  "$(jq -n --arg sid "$SESSION_ID" '{
    hook_event_name: "PostToolUseFailure",
    session_id: $sid,
    tool_name: "Bash",
    cwd: "/tmp/test"
  }')" \
  "thinking"

run_case "Stop → idle" \
  "$(jq -n --arg sid "$SESSION_ID" '{
    hook_event_name: "Stop",
    session_id: $sid,
    cwd: "/tmp/test"
  }')" \
  "idle"

# ── New: PermissionRequest ────────────────────────────────────────────────────

run_case "PermissionRequest → blocked" \
  "$(jq -n --arg sid "$SESSION_ID" '{
    hook_event_name: "PermissionRequest",
    session_id: $sid,
    tool_name: "Bash",
    tool_input: {command: "rm -rf /tmp/test"},
    cwd: "/tmp/test"
  }')" \
  "blocked"

# ── New: Notification subtypes ────────────────────────────────────────────────

run_case "Notification/idle_prompt → needs_user" \
  "$(jq -n --arg sid "$SESSION_ID" '{
    hook_event_name: "Notification",
    session_id: $sid,
    notification_type: "idle_prompt",
    message: "Claude is waiting for your input",
    cwd: "/tmp/test"
  }')" \
  "needs_user"

run_case "Notification/elicitation_dialog → needs_user" \
  "$(jq -n --arg sid "$SESSION_ID" '{
    hook_event_name: "Notification",
    session_id: $sid,
    notification_type: "elicitation_dialog",
    message: "Claude is asking a question",
    cwd: "/tmp/test"
  }')" \
  "needs_user"

run_case "Notification/permission_prompt → blocked" \
  "$(jq -n --arg sid "$SESSION_ID" '{
    hook_event_name: "Notification",
    session_id: $sid,
    notification_type: "permission_prompt",
    message: "Permission needed",
    cwd: "/tmp/test"
  }')" \
  "blocked"

# ── Edge cases: must be ignored ───────────────────────────────────────────────

run_case "Notification/auth_success → ignored" \
  "$(jq -n --arg sid "$SESSION_ID" '{
    hook_event_name: "Notification",
    session_id: $sid,
    notification_type: "auth_success",
    message: "Auth succeeded",
    cwd: "/tmp/test"
  }')" \
  ""  # expect no output

run_case "Unknown event → ignored" \
  "$(jq -n --arg sid "$SESSION_ID" '{
    hook_event_name: "SubagentStart",
    session_id: $sid,
    cwd: "/tmp/test"
  }')" \
  ""  # expect no output

run_case "Missing session_id → ignored" \
  "$(jq -n '{
    hook_event_name: "UserPromptSubmit",
    cwd: "/tmp/test"
  }')" \
  ""  # expect no output

# ── tool_name in blocked outbox ───────────────────────────────────────────────

echo ""
echo "─────────────────────────────────────────────────────"
echo "Checking PermissionRequest carries tool_name in outbox..."

touch /tmp/probe-hook-start
printf '%s' "$(jq -n --arg sid "$SESSION_ID" '{
  hook_event_name: "PermissionRequest",
  session_id: $sid,
  tool_name: "Bash",
  cwd: "/tmp/test"
}')" | bash "$HOOK" 2>/dev/null

written=$(find "$OUTBOX" -name "prs.*.json" -newer /tmp/probe-hook-start 2>/dev/null | sort | tail -1)
if [[ -n "$written" ]]; then
  tool_in_file=$(jq -r '.tool_name' "$written" 2>/dev/null || echo "")
  rm -f "$written"
  if [[ "$tool_in_file" == "Bash" ]]; then
    echo "PASS  PermissionRequest outbox has tool_name=Bash"
    ((PASS++)) || true
  else
    echo "FAIL  PermissionRequest outbox tool_name='$tool_in_file' (expected Bash)"
    ((FAIL++)) || true
  fi
else
  echo "FAIL  No outbox file written for tool_name check"
  ((FAIL++)) || true
fi

# ── Summary ───────────────────────────────────────────────────────────────────

echo ""
echo "─────────────────────────────────────────────────────"
echo "Results: $PASS passed, $FAIL failed"
echo ""

[[ "$FAIL" -eq 0 ]] && exit 0 || exit 1
