#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

FAKE_HELPER="$TMP_DIR/fake-hosted-instance.sh"
cat >"$FAKE_HELPER" <<'EOF'
#!/usr/bin/env bash
lh_hosted_resolve_instance() {
  LH_INSTANCE_ID="7"
  LH_INSTANCE_URL="https://demo.longhouse.ai"
  LH_INSTANCE_SUBDOMAIN="$1"
  LH_INSTANCE_STATUS="active"
  LH_INSTANCE_CONTAINER_NAME="longhouse-$1"
  LH_INSTANCE_DATA_PATH="/srv/longhouse/$1"
  export LH_INSTANCE_ID LH_INSTANCE_URL LH_INSTANCE_SUBDOMAIN LH_INSTANCE_STATUS LH_INSTANCE_CONTAINER_NAME LH_INSTANCE_DATA_PATH
}

lh_hosted_get_instance() {
  :
}
EOF
chmod +x "$FAKE_HELPER"

mkdir -p "$TMP_DIR/bin"

cat >"$TMP_DIR/bin/ssh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
target="$1"
shift
cmd="$*"
if [[ "$target" != "fake-zerg" ]]; then
  echo "Unexpected ssh target: $target" >&2
  exit 1
fi
if [[ "$cmd" == python3\ -* ]]; then
  printf '%s' '{
    "db_path": "/srv/longhouse/demo/longhouse.db",
    "session_id": "sess-1",
        "tables": {
          "sessions": true,
          "events": true,
          "session_observations": true,
          "session_runtime_state": true,
          "session_turns": true
        },
    "session": {
      "id": "sess-1",
      "provider": "claude",
      "project": "zerg",
      "device_id": "cinder",
      "cwd": "/Users/example/git/zerg",
      "started_at": "2026-04-27 20:35:26",
      "ended_at": "2026-04-27 22:12:06",
      "last_activity_at": "2026-04-27 22:12:06",
      "execution_home": "managed_local",
      "managed_transport": "claude_channel_bridge",
      "source_runner_name": "cinder",
      "transcript_revision": 180,
      "summary_revision": 180,
      "embedding_revision": 180
    },
    "runtime_state": {
      "runtime_key": "claude:sess-1",
      "phase": "running",
      "phase_source": "semantic",
      "active_tool": "Bash",
      "last_runtime_signal_at": "2026-04-27 22:12:07",
      "last_progress_at": "2026-04-27 22:12:06",
      "last_live_at": "2026-04-27 22:12:07",
      "freshness_expires_at": "2026-04-27 22:22:07",
      "terminal_state": null,
      "terminal_at": null,
      "runtime_version": 320,
      "updated_at": "2026-04-27 22:12:07"
    },
    "event_stats": {
      "count": 366,
      "first_timestamp": "2026-04-27 20:37:57",
      "last_timestamp": "2026-04-27 22:10:48",
      "assistant_events": 54,
      "tool_events": 144,
      "tool_call_events": 144
    },
        "runtime_observation_stats": {
          "count": 421,
          "first_observed_at": "2026-04-27 20:35:26",
          "last_observed_at": "2026-04-27 22:10:49",
          "first_received_at": "2026-04-27 20:35:26",
          "last_received_at": "2026-04-27 22:10:50"
        },
        "recent_runtime_observations": [
          {"kind": "phase_signal", "phase": "running", "tool_name": "Bash", "observed_at": "2026-04-27 22:10:27", "received_at": "2026-04-27 22:10:27", "freshness_ms": 600000}
        ],
    "recent_events": [
      {"id": 5332839, "role": "tool", "tool_name": null, "text": "The file was updated", "timestamp": "2026-04-27 22:10:48"}
    ],
    "recent_turns": [
      {"state": "durable", "timing_confidence": "inferred", "terminal_phase": null, "created_at": "2026-04-27 21:38:29", "durable_at": "2026-04-27 21:38:26"}
    ]
  }'
  exit 0
fi
if [[ "$cmd" == *"docker logs"* && "$cmd" == *"grep -F"* ]]; then
  printf '%s\n' 'session sess-1 log line'
  exit 0
fi
if [[ "$cmd" == *"docker logs"* ]]; then
  printf '%s\n' '2026-04-27 WARNING WriteSerializer: ingest waited 20ms in queue, exec 80ms'
  printf '%s\n' 'POST /api/agents/ingest HTTP/1.1'
  printf '%s\n' 'POST /api/agents/runtime/events/batch HTTP/1.1'
  exit 0
fi
echo "Unexpected ssh command: $cmd" >&2
exit 1
EOF
chmod +x "$TMP_DIR/bin/ssh"

TEXT_OUTPUT="$TMP_DIR/text.txt"
JSON_OUTPUT="$TMP_DIR/out.json"

PATH="$TMP_DIR/bin:$PATH" \
HOSTED_INSTANCE_HELPER="$FAKE_HELPER" \
HOSTED_SESSION_DEBUG_SSH_TARGET="fake-zerg" \
bash "$ROOT_DIR/ops/hosted-session-debug.sh" --subdomain demo --session sess-1 --limit 2 --logs >"$TEXT_OUTPUT"

if ! grep -q "host_data_path: /srv/longhouse/demo" "$TEXT_OUTPUT"; then
  echo "Expected text output to include helper-provided data path"
  exit 1
fi

if ! grep -q "managed_transport: claude_channel_bridge" "$TEXT_OUTPUT"; then
  echo "Expected text output to include session management fields"
  exit 1
fi

if ! grep -q "write_serializer:" "$TEXT_OUTPUT"; then
  echo "Expected text output to include write serializer summary"
  exit 1
fi

PATH="$TMP_DIR/bin:$PATH" \
HOSTED_INSTANCE_HELPER="$FAKE_HELPER" \
HOSTED_SESSION_DEBUG_SSH_TARGET="fake-zerg" \
bash "$ROOT_DIR/ops/hosted-session-debug.sh" --subdomain demo --session sess-1 --limit 2 --logs --json >"$JSON_OUTPUT"

python3 - "$JSON_OUTPUT" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["instance"]["subdomain"] == "demo"
assert payload["instance"]["host_data_path"] == "/srv/longhouse/demo"
assert payload["database"]["session"]["id"] == "sess-1"
assert payload["database"]["runtime_state"]["phase"] == "running"
assert payload["log_counts"]["agents_ingest"] == 1
assert payload["logs"] == ["session sess-1 log line"]
PY

echo "hosted-session-debug helper tests passed"
