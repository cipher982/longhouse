#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOSTED_INSTANCE_HELPER="$ROOT_DIR/scripts/lib/hosted-instance.sh"

if [[ ! -f "$HOSTED_INSTANCE_HELPER" ]]; then
  echo "Hosted instance helper missing: $HOSTED_INSTANCE_HELPER" >&2
  exit 1
fi

# shellcheck disable=SC1090
. "$HOSTED_INSTANCE_HELPER"

INSTANCE_SUBDOMAIN="david010"
SESSION_ID=""
LIMIT=10
SHOW_LOGS="false"

usage() {
  cat <<'EOF'
Usage:
  scripts/hosted-loop-debug.sh [subdomain]
  scripts/hosted-loop-debug.sh --subdomain david010 --session <session-id> [--limit 5] [--logs]

What it does:
  1. Resolves the hosted tenant through the control plane
  2. Authenticates a browser cookie against the tenant
  3. Fetches /api/oikos/loop-inbox and /api/oikos/turn-reviews
  4. Queries /data/longhouse.db inside the running tenant container

Requirements:
  - CONTROL_PLANE_ADMIN_TOKEN (or ADMIN_TOKEN)
  - SSH access to host alias "zerg"

Options:
  --subdomain <name>   Hosted instance subdomain (default: david010)
  --session <id>       Narrow output to one session
  --limit <n>          Max rows to show per section (default: 10)
  --logs               Also tail loop-related tenant logs
  -h, --help           Show help
EOF
}

while (($# > 0)); do
  case "$1" in
    --subdomain)
      [[ -n "${2:-}" ]] || { echo "--subdomain requires a value" >&2; exit 1; }
      INSTANCE_SUBDOMAIN="$2"
      shift 2
      ;;
    --session)
      [[ -n "${2:-}" ]] || { echo "--session requires a value" >&2; exit 1; }
      SESSION_ID="$2"
      shift 2
      ;;
    --limit)
      [[ -n "${2:-}" ]] || { echo "--limit requires a value" >&2; exit 1; }
      LIMIT="$2"
      shift 2
      ;;
    --logs)
      SHOW_LOGS="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --*)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 1
      ;;
    *)
      if [[ "$INSTANCE_SUBDOMAIN" != "david010" ]]; then
        echo "Unexpected extra argument: $1" >&2
        usage >&2
        exit 1
      fi
      INSTANCE_SUBDOMAIN="$1"
      shift
      ;;
  esac
done

if ! [[ "$LIMIT" =~ ^[0-9]+$ ]] || [[ "$LIMIT" -lt 1 ]]; then
  echo "--limit must be a positive integer" >&2
  exit 1
fi

cleanup() {
  if [[ -n "${COOKIE_JAR:-}" ]]; then
    rm -f "$COOKIE_JAR"
  fi
  if [[ -n "${CARD_FILE:-}" ]]; then
    rm -f "$CARD_FILE"
  fi
  if [[ -n "${LOOP_FILE:-}" ]]; then
    rm -f "$LOOP_FILE"
  fi
  if [[ -n "${REVIEWS_FILE:-}" ]]; then
    rm -f "$REVIEWS_FILE"
  fi
}
trap cleanup EXIT

print_header() {
  printf '\n== %s ==\n' "$1"
}

lh_hosted_resolve_instance "$INSTANCE_SUBDOMAIN"
lh_hosted_get_instance "$LH_INSTANCE_ID"

INSTANCE_URL="$LH_INSTANCE_URL"
CONTAINER_NAME="${LH_INSTANCE_CONTAINER_NAME:-}"

if [[ -z "$CONTAINER_NAME" ]]; then
  echo "Control-plane response did not include a container name for $INSTANCE_SUBDOMAIN" >&2
  exit 1
fi

COOKIE_JAR="$(mktemp)"
lh_hosted_authenticate_cookie_jar "$INSTANCE_SUBDOMAIN" "$COOKIE_JAR"

print_header "Instance"
cat <<EOF
subdomain: $INSTANCE_SUBDOMAIN
instance_id: ${LH_INSTANCE_ID}
url: ${INSTANCE_URL}
status: ${LH_INSTANCE_STATUS:-unknown}
container: ${CONTAINER_NAME}
host_data_path: /var/app-data/longhouse/${INSTANCE_SUBDOMAIN}
container_db_path: /data/longhouse.db
EOF

print_header "API Loop Inbox"
if [[ -n "$SESSION_ID" ]]; then
  CARD_FILE="$(mktemp)"
  status_code="$(curl -sS -o "$CARD_FILE" -w "%{http_code}" \
    -b "$COOKIE_JAR" \
    "${INSTANCE_URL%/}/api/oikos/loop-inbox/${SESSION_ID}")"
  if [[ "$status_code" == "200" ]]; then
    python3 - "$CARD_FILE" <<'PY'
import json
import pathlib
import sys

payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(
    f"session={payload.get('session_id')} title={payload.get('title')!r} "
    f"decision={payload.get('decision')} execution={payload.get('execution_state')} "
    f"recommended={payload.get('recommended_action')} "
    f"follow_up={payload.get('follow_up_prompt')!r}"
)
print(f"summary={payload.get('summary')!r}")
blocked = payload.get("blocked_reasons") or []
if blocked:
    print(f"blocked_reasons={blocked}")
print(f"available_actions={payload.get('available_actions') or []}")
PY
  elif [[ "$status_code" == "404" ]]; then
    echo "No attention-worthy loop inbox card for session $SESSION_ID"
  else
    echo "Loop inbox card request failed (HTTP $status_code)" >&2
    cat "$CARD_FILE" >&2
    exit 1
  fi
  rm -f "$CARD_FILE"
  unset CARD_FILE
else
  LOOP_FILE="$(mktemp)"
  status_code="$(curl -sS -o "$LOOP_FILE" -w "%{http_code}" \
    -b "$COOKIE_JAR" \
    "${INSTANCE_URL%/}/api/oikos/loop-inbox?limit=${LIMIT}")"
  if [[ "$status_code" != "200" ]]; then
    echo "Loop inbox request failed (HTTP $status_code)" >&2
    cat "$LOOP_FILE" >&2
    exit 1
  fi
  python3 - "$LOOP_FILE" <<'PY'
import json
import pathlib
import sys

rows = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"count={len(rows)}")
for row in rows:
    print(
        f"session={row.get('session_id')} title={row.get('title')!r} "
        f"decision={row.get('decision')} execution={row.get('execution_state')} "
        f"recommended={row.get('recommended_action')} "
        f"follow_up={row.get('follow_up_prompt')!r}"
    )
    print(f"  summary={row.get('summary')!r}")
PY
  rm -f "$LOOP_FILE"
  unset LOOP_FILE
fi

print_header "API Turn Reviews"
TURN_REVIEW_URL="${INSTANCE_URL%/}/api/oikos/turn-reviews?limit=${LIMIT}"
if [[ -n "$SESSION_ID" ]]; then
  TURN_REVIEW_URL="${TURN_REVIEW_URL}&session_id=${SESSION_ID}"
fi
REVIEWS_FILE="$(mktemp)"
status_code="$(curl -sS -o "$REVIEWS_FILE" -w "%{http_code}" -b "$COOKIE_JAR" "$TURN_REVIEW_URL")"
if [[ "$status_code" != "200" ]]; then
  echo "Turn reviews request failed (HTTP $status_code)" >&2
  cat "$REVIEWS_FILE" >&2
  exit 1
fi
python3 - "$REVIEWS_FILE" <<'PY'
import json
import pathlib
import sys

rows = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
print(f"count={len(rows)}")
for row in rows:
    print(
        f"id={row.get('id')} session={row.get('session_id')} "
        f"decision={row.get('decision')} execution={row.get('execution_state')} "
        f"status={row.get('status')} reason={row.get('reason')!r}"
    )
    print(f"  summary={row.get('summary')!r}")
    if row.get("follow_up_prompt"):
        print(f"  follow_up_prompt={row.get('follow_up_prompt')!r}")
PY
rm -f "$REVIEWS_FILE"
unset REVIEWS_FILE

print_header "Container SQLite"
ssh zerg "docker exec -i '$CONTAINER_NAME' python3 - '$SESSION_ID' '$LIMIT'" <<'PY'
import sqlite3
import sys

session_id = sys.argv[1] or None
limit = int(sys.argv[2])

conn = sqlite3.connect("/data/longhouse.db")
sql = """
SELECT id, session_id, decision, execution_state, status, reason, summary, follow_up_prompt
FROM session_turn_reviews
"""
params = []
if session_id:
    sql += " WHERE session_id = ?"
    params.append(session_id)
sql += " ORDER BY id DESC LIMIT ?"
params.append(limit)
rows = conn.execute(sql, params).fetchall()
print(f"count={len(rows)}")
for row in rows:
    review_id, sid, decision, execution_state, status, reason, summary, follow_up_prompt = row
    summary = (summary or "").replace("\n", " ").strip()
    follow_up_prompt = (follow_up_prompt or "").replace("\n", " ").strip() or None
    print(
        f"id={review_id} session={sid} decision={decision} execution={execution_state} "
        f"status={status} reason={reason!r}"
    )
    print(f"  summary={summary[:200]!r}")
    if follow_up_prompt:
        print(f"  follow_up_prompt={follow_up_prompt[:200]!r}")
PY

if [[ "$SHOW_LOGS" == "true" ]]; then
  print_header "Tenant Logs"
  ssh zerg "docker logs --since 30m '$CONTAINER_NAME' 2>&1 | grep -niE 'loop|turn review|session_turn_reviews|operator-turn|Needs approval|follow-up|supersed' | tail -n 40 || true"
fi
