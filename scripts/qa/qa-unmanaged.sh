#!/usr/bin/env bash
# Smoke-test unmanaged compatibility ingest using the real provider CLIs.
#
# Strategy:
#   1. Run bare `claude` and bare `codex` headlessly for one short turn.
#   2. Resolve the real transcript file each CLI persisted.
#   3. Force deterministic ingest with `longhouse-engine ship --file` so the QA
#      does not depend on watcher timing or fallback discovery cadence.
#   4. Verify the session shows up on the timeline and remains unmanaged
#      (no live control / no host reattach).

set -euo pipefail

CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
if [[ "$(basename "$CLAUDE_DIR")" == ".longhouse" ]]; then
  LONGHOUSE_HOME="${LONGHOUSE_HOME:-$CLAUDE_DIR}"
else
  LONGHOUSE_HOME="${LONGHOUSE_HOME:-$(dirname "$CLAUDE_DIR")/.longhouse}"
fi

STATE_FILE="${LONGHOUSE_HOME}/machine/state.json"
TOKEN_FILE="${LONGHOUSE_HOME}/machine/device-token"

API_URL="${API_URL:-}"
DEVICE_TOKEN="${DEVICE_TOKEN:-}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude 2>/dev/null || true)}"
CODEX_BIN="${CODEX_BIN:-$(command -v codex 2>/dev/null || true)}"
ENGINE_BIN="${ENGINE_BIN:-$(command -v longhouse-engine 2>/dev/null || true)}"
KEEP_TMP="${KEEP_TMP:-0}"

TMP_ROOT="$(mktemp -d /tmp/lh-qa-unmanaged.XXXXXX)"
PASS_COUNT=0
FAIL_COUNT=0

CLAUDE_SESSION_ID=""
CLAUDE_TRANSCRIPT=""
CODEX_SESSION_ID=""
CODEX_TRANSCRIPT=""

cleanup() {
  local rc=$?
  if [[ "$KEEP_TMP" == "1" || $rc -ne 0 || $FAIL_COUNT -gt 0 ]]; then
    echo "Debug files preserved at $TMP_ROOT"
  else
    rm -rf "$TMP_ROOT"
  fi
}
trap cleanup EXIT

pass() { echo "  PASS: $1"; PASS_COUNT=$((PASS_COUNT + 1)); }
fail() { echo "  FAIL: $1" >&2; FAIL_COUNT=$((FAIL_COUNT + 1)); }
section() { echo ""; echo "=== $1 ==="; }

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    fail "missing required command: $1"
    exit 1
  fi
}

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    fail "missing required file: $path"
    exit 1
  fi
}

resolve_runtime() {
  require_cmd jq
  require_cmd curl
  require_cmd find
  require_cmd mktemp

  [[ -n "$CLAUDE_BIN" ]] || { fail "claude binary not found"; exit 1; }
  [[ -n "$CODEX_BIN" ]] || { fail "codex binary not found"; exit 1; }
  [[ -n "$ENGINE_BIN" ]] || { fail "longhouse-engine binary not found"; exit 1; }

  require_file "$STATE_FILE"
  require_file "$TOKEN_FILE"

  if [[ -z "$API_URL" ]]; then
    API_URL="$(jq -r '.runtime_url // empty' "$STATE_FILE" | tr -d '[:space:]')"
  fi
  if [[ -z "$DEVICE_TOKEN" ]]; then
    DEVICE_TOKEN="$(tr -d '[:space:]' < "$TOKEN_FILE")"
  fi

  [[ -n "$API_URL" ]] || { fail "runtime_url missing in $STATE_FILE"; exit 1; }
  [[ -n "$DEVICE_TOKEN" ]] || { fail "device token missing in $TOKEN_FILE"; exit 1; }

  if curl -sf "$API_URL/api/health" >/dev/null; then
    pass "API reachable at $API_URL"
  else
    fail "API unreachable at $API_URL/api/health"
    exit 1
  fi
}

wait_for_transcript() {
  local provider="$1"
  local session_id="$2"
  local root=""
  local pattern=""
  local found=""
  local attempt=""

  case "$provider" in
    claude)
      root="${CLAUDE_DIR}/projects"
      pattern="${session_id}.jsonl"
      ;;
    codex)
      root="${HOME}/.codex/sessions"
      pattern="*${session_id}.jsonl"
      ;;
    *)
      return 1
      ;;
  esac

  for attempt in $(seq 1 20); do
    found="$(find "$root" -type f -name "$pattern" -print -quit 2>/dev/null || true)"
    if [[ -n "$found" ]]; then
      printf '%s\n' "$found"
      return 0
    fi
    sleep 0.25
  done

  return 1
}

wait_for_session_detail() {
  local session_id="$1"
  local out_file="$2"
  local attempt=""

  for attempt in $(seq 1 20); do
    if curl -sf -H "X-Agents-Token: $DEVICE_TOKEN" \
      "$API_URL/api/agents/sessions/$session_id" >"$out_file"; then
      return 0
    fi
    sleep 0.25
  done

  return 1
}

run_claude_session() {
  local workdir="$TMP_ROOT/claude-workdir"
  local output_jsonl="$TMP_ROOT/claude-output.jsonl"
  local stderr_log="$TMP_ROOT/claude-stderr.log"
  local prompt="Reply with exactly CLAUDE-QA-OK and nothing else."
  local reply=""

  mkdir -p "$workdir"

  if (
    cd "$workdir" &&
      printf '%s\n' "$prompt" | "$CLAUDE_BIN" \
        --print \
        --verbose \
        --output-format stream-json \
        --include-hook-events \
        --permission-mode default
  ) >"$output_jsonl" 2>"$stderr_log"; then
    pass "bare claude run completed"
  else
    fail "bare claude run failed"
    tail -n 40 "$stderr_log" >&2 || true
    return 1
  fi

  CLAUDE_SESSION_ID="$(jq -rs '
    (
      (map(select(.type == "result") | .session_id) | last) //
      (map(select(.type == "system" and .subtype == "init") | .session_id) | first) //
      ""
    )
  ' "$output_jsonl")"
  reply="$(jq -rs '(map(select(.type == "result") | .result) | last) // ""' "$output_jsonl")"

  [[ -n "$CLAUDE_SESSION_ID" ]] || { fail "could not parse Claude session id"; return 1; }
  [[ "$reply" == "CLAUDE-QA-OK" ]] || { fail "unexpected Claude reply: ${reply:-<empty>}"; return 1; }
  pass "claude session id captured ($CLAUDE_SESSION_ID)"
  pass "claude replied with expected text"

  CLAUDE_TRANSCRIPT="$(wait_for_transcript claude "$CLAUDE_SESSION_ID")" || {
    fail "could not find Claude transcript for $CLAUDE_SESSION_ID"
    return 1
  }
  pass "claude transcript resolved"
}

run_codex_session() {
  local workdir="$TMP_ROOT/codex-workdir"
  local output_jsonl="$TMP_ROOT/codex-output.jsonl"
  local stderr_log="$TMP_ROOT/codex-stderr.log"
  local prompt="Reply with exactly CODEX-QA-OK and nothing else."
  local reply=""

  mkdir -p "$workdir"

  if "$CODEX_BIN" exec \
    --skip-git-repo-check \
    -C "$workdir" \
    -s read-only \
    --json \
    "$prompt" \
    </dev/null >"$output_jsonl" 2>"$stderr_log"; then
    pass "bare codex run completed"
  else
    fail "bare codex run failed"
    tail -n 40 "$stderr_log" >&2 || true
    return 1
  fi

  CODEX_SESSION_ID="$(jq -rs '(map(select(.type == "thread.started") | .thread_id) | first) // ""' "$output_jsonl")"
  reply="$(jq -rs '
    (
      map(select(.type == "item.completed" and .item.type == "agent_message") | .item.text) | last
    ) // ""
  ' "$output_jsonl")"

  [[ -n "$CODEX_SESSION_ID" ]] || { fail "could not parse Codex session id"; return 1; }
  [[ "$reply" == "CODEX-QA-OK" ]] || { fail "unexpected Codex reply: ${reply:-<empty>}"; return 1; }
  pass "codex session id captured ($CODEX_SESSION_ID)"
  pass "codex replied with expected text"

  CODEX_TRANSCRIPT="$(wait_for_transcript codex "$CODEX_SESSION_ID")" || {
    fail "could not find Codex transcript for $CODEX_SESSION_ID"
    return 1
  }
  pass "codex transcript resolved"
}

ship_and_assert() {
  local provider="$1"
  local session_id="$2"
  local transcript="$3"
  local expected_reply="$4"
  local ship_json="$TMP_ROOT/${provider}-ship.json"
  local ship_stderr="$TMP_ROOT/${provider}-ship.stderr"
  local session_json="$TMP_ROOT/${provider}-session.json"
  local list_json="$TMP_ROOT/${provider}-timeline.json"
  local events_json="$TMP_ROOT/${provider}-events.json"
  local events_shipped=""

  if "$ENGINE_BIN" ship \
    --file "$transcript" \
    --provider "$provider" \
    --url "$API_URL" \
    --token "$DEVICE_TOKEN" \
    --json \
    --require-reply-evidence \
    >"$ship_json" 2>"$ship_stderr"; then
    pass "explicit ${provider} ship completed"
  else
    fail "explicit ${provider} ship failed"
    tail -n 40 "$ship_stderr" >&2 || true
    return 1
  fi

  events_shipped="$(jq -r '.events_shipped // 0' "$ship_json")"
  echo "  INFO: ${provider} ship events_shipped=${events_shipped}"

  if wait_for_session_detail "$session_id" "$session_json"; then
    pass "${provider} session visible via detail API"
  else
    fail "${provider} session did not appear via detail API"
    return 1
  fi

  curl -sf -H "X-Agents-Token: $DEVICE_TOKEN" \
    "$API_URL/api/agents/sessions?limit=100&days_back=1" >"$list_json"
  curl -sf -H "X-Agents-Token: $DEVICE_TOKEN" \
    "$API_URL/api/agents/sessions/$session_id/events" >"$events_json"

  if jq -e --arg sid "$session_id" '.sessions | map(.id) | index($sid) != null' "$list_json" >/dev/null; then
    pass "${provider} session appears on the timeline list"
  else
    fail "${provider} session missing from timeline list"
    return 1
  fi

  if jq -e --arg provider "$provider" '
    .provider == $provider and
    .control == null and
    .continuation_kind == "local" and
    (.capabilities.live_control_available == false) and
    (.capabilities.host_reattach_available == false) and
    (.capabilities.reply_to_live_session_available == false) and
    ((.user_messages // 0) >= 1) and
    ((.assistant_messages // 0) >= 1)
  ' "$session_json" >/dev/null; then
    pass "${provider} session remains unmanaged"
  else
    fail "${provider} session metadata did not match unmanaged expectations"
    jq '.' "$session_json" >&2 || true
    return 1
  fi

  if jq -e --arg reply "$expected_reply" '
    ((if type == "object" then .events else . end) // [])
    | any(.[]?; .role == "assistant" and (.content_text // "") == $reply)
  ' "$events_json" >/dev/null; then
    pass "${provider} reply is present in shipped events"
  else
    fail "${provider} shipped events missing expected reply"
    jq '.' "$events_json" >&2 || true
    return 1
  fi
}

section "Prerequisites"
resolve_runtime

section "Bare Claude"
run_claude_session
ship_and_assert claude "$CLAUDE_SESSION_ID" "$CLAUDE_TRANSCRIPT" "CLAUDE-QA-OK"

section "Bare Codex"
run_codex_session
ship_and_assert codex "$CODEX_SESSION_ID" "$CODEX_TRANSCRIPT" "CODEX-QA-OK"

section "Summary"
TOTAL=$((PASS_COUNT + FAIL_COUNT))
echo ""
echo "$PASS_COUNT/$TOTAL checks passed"

if [[ "$FAIL_COUNT" -gt 0 ]]; then
  echo "FAILED"
  exit 1
fi

echo "OK"
