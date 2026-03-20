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

lh_hosted_authenticate_cookie_jar() {
  : > "$2"
}
EOF
chmod +x "$FAKE_HELPER"

mkdir -p "$TMP_DIR/bin"

cat >"$TMP_DIR/bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
output_file=""
write_format=""
url=""
while (($# > 0)); do
  case "$1" in
    -o)
      output_file="$2"
      shift 2
      ;;
    -w)
      write_format="$2"
      shift 2
      ;;
    -b|-H|-d|-X|--connect-timeout|--max-time)
      shift 2
      ;;
    -s|-S|-f|-sS|-fsS)
      shift
      ;;
    *)
      url="$1"
      shift
      ;;
  esac
done

payload='{}'
if [[ "$url" == *"/api/oikos/loop-inbox/sess-1" ]]; then
  payload='{"session_id":"sess-1","title":"Hiring loop","decision":"wait","execution_state":"needs_human","recommended_action":"wait","follow_up_prompt":null,"summary":"Need human input","blocked_reasons":["missing context"],"available_actions":["not_now","open_full_session"]}'
elif [[ "$url" == *"/api/oikos/loop-inbox?limit=2" ]]; then
  payload='[{"session_id":"sess-1","title":"Hiring loop","decision":"wait","execution_state":"needs_human","recommended_action":"wait","follow_up_prompt":null,"summary":"Need human input"}]'
elif [[ "$url" == *"/api/oikos/turn-reviews?limit=2&session_id=sess-1" ]]; then
  payload='[{"id":12,"session_id":"sess-1","decision":"wait","execution_state":"needs_human","status":"enqueued","reason":"notify_user","summary":"Need human input","follow_up_prompt":null}]'
elif [[ "$url" == *"/api/oikos/turn-reviews?limit=2" ]]; then
  payload='[{"id":12,"session_id":"sess-1","decision":"wait","execution_state":"needs_human","status":"enqueued","reason":"notify_user","summary":"Need human input","follow_up_prompt":null}]'
fi

if [[ -n "$output_file" ]]; then
  printf '%s' "$payload" > "$output_file"
else
  printf '%s' "$payload"
fi

if [[ -n "$write_format" ]]; then
  printf '200'
fi
EOF
chmod +x "$TMP_DIR/bin/curl"

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
if [[ "$cmd" == *"docker exec -i"* ]]; then
  printf '%s' '[{"id":12,"session_id":"sess-1","decision":"wait","execution_state":"needs_human","status":"enqueued","reason":"notify_user","summary":"Need human input","follow_up_prompt":null}]'
  exit 0
fi
if [[ "$cmd" == *"docker logs"* ]]; then
  printf '%s\n' '101: loop debug log line'
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
HOSTED_LOOP_DEBUG_SSH_TARGET="fake-zerg" \
bash "$ROOT_DIR/hosted-loop-debug.sh" --subdomain demo --session sess-1 --limit 2 --logs >"$TEXT_OUTPUT"

if ! grep -q "host_data_path: /srv/longhouse/demo" "$TEXT_OUTPUT"; then
  echo "Expected text output to include helper-provided data path"
  exit 1
fi

if ! grep -q "available_actions=\['not_now', 'open_full_session'\]" "$TEXT_OUTPUT"; then
  echo "Expected text output to include loop action card actions"
  exit 1
fi

PATH="$TMP_DIR/bin:$PATH" \
HOSTED_INSTANCE_HELPER="$FAKE_HELPER" \
HOSTED_LOOP_DEBUG_SSH_TARGET="fake-zerg" \
bash "$ROOT_DIR/hosted-loop-debug.sh" --subdomain demo --session sess-1 --limit 2 --logs --json >"$JSON_OUTPUT"

python3 - "$JSON_OUTPUT" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
assert payload["instance"]["subdomain"] == "demo"
assert payload["instance"]["host_data_path"] == "/srv/longhouse/demo"
assert payload["loop_action_card"]["session_id"] == "sess-1"
assert payload["turn_reviews"][0]["id"] == 12
assert payload["sqlite_reviews"][0]["status"] == "enqueued"
assert payload["logs"] == ["101: loop debug log line"]
PY

echo "hosted-loop-debug helper tests passed"
