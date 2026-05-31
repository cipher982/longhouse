#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
HOSTED_INSTANCE_HELPER="${HOSTED_INSTANCE_HELPER:-$ROOT_DIR/scripts/lib/hosted-instance.sh}"
SSH_TARGET="${HOSTED_SESSION_DEBUG_SSH_TARGET:-runtime-host}"

if [[ ! -f "$HOSTED_INSTANCE_HELPER" ]]; then
  echo "Hosted instance helper missing: $HOSTED_INSTANCE_HELPER" >&2
  exit 1
fi

# shellcheck disable=SC1090
. "$HOSTED_INSTANCE_HELPER"

INSTANCE_SUBDOMAIN="${LONGHOUSE_DEFAULT_SUBDOMAIN:-demo}"
SESSION_ID=""
LIMIT=20
LOGS_SINCE="30m"
SHOW_LOGS="false"
OUTPUT_MODE="text"

usage() {
  cat <<'EOF'
Usage:
  scripts/ops/hosted-session-debug.sh --session <session-id> [--subdomain <name>] [--limit 20] [--logs] [--json]

What it does:
  1. Resolves the hosted tenant through the control plane
  2. Queries the tenant SQLite database on the host data path
  3. Summarizes session/runtime/event state and recent write pressure
  4. Optionally tails session-specific tenant logs

Requirements:
  - CONTROL_PLANE_ADMIN_TOKEN (or ADMIN_TOKEN)
  - SSH access to host alias "runtime-host" (override with HOSTED_SESSION_DEBUG_SSH_TARGET)

Options:
  --subdomain <name>   Hosted instance subdomain (default: $LONGHOUSE_DEFAULT_SUBDOMAIN or demo)
  --session <id>       Session ID to inspect (required)
  --limit <n>          Max recent rows to show per section (default: 20)
  --logs               Include session-specific tenant logs
  --logs-since <dur>   Docker log window when --logs is enabled (default: 30m)
  --json               Emit one JSON payload instead of human-readable sections
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
    --logs-since)
      [[ -n "${2:-}" ]] || { echo "--logs-since requires a value" >&2; exit 1; }
      LOGS_SINCE="$2"
      shift 2
      ;;
    --json)
      OUTPUT_MODE="json"
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
      if [[ "$INSTANCE_SUBDOMAIN" != "${LONGHOUSE_DEFAULT_SUBDOMAIN:-demo}" ]]; then
        echo "Unexpected extra argument: $1" >&2
        usage >&2
        exit 1
      fi
      INSTANCE_SUBDOMAIN="$1"
      shift
      ;;
  esac
done

if [[ -z "$SESSION_ID" ]]; then
  echo "--session is required" >&2
  usage >&2
  exit 1
fi

if ! [[ "$LIMIT" =~ ^[0-9]+$ ]] || [[ "$LIMIT" -lt 1 ]]; then
  echo "--limit must be a positive integer" >&2
  exit 1
fi

cleanup() {
  local path=""
  for path in "${SQLITE_FILE:-}" "${LOGS_FILE:-}" "${COUNTS_FILE:-}" "${RAW_LOGS_FILE:-}"; do
    if [[ -n "$path" ]]; then
      rm -f "$path"
    fi
  done
}
trap cleanup EXIT

print_header() {
  printf '\n== %s ==\n' "$1"
}

lh_hosted_resolve_instance "$INSTANCE_SUBDOMAIN"
lh_hosted_get_instance "$LH_INSTANCE_ID"

INSTANCE_URL="$LH_INSTANCE_URL"
CONTAINER_NAME="${LH_INSTANCE_CONTAINER_NAME:-}"
HOST_DATA_PATH="${LH_INSTANCE_DATA_PATH:-/var/app-data/longhouse/${INSTANCE_SUBDOMAIN}}"
HOST_DB_PATH="${HOST_DATA_PATH%/}/longhouse.db"

if [[ -z "$CONTAINER_NAME" ]]; then
  echo "Control-plane response did not include a container name for $INSTANCE_SUBDOMAIN" >&2
  exit 1
fi

SQLITE_FILE="$(mktemp)"
ssh "$SSH_TARGET" "python3 - '$HOST_DB_PATH' '$SESSION_ID' '$LIMIT'" > "$SQLITE_FILE" <<'PY'
import json
import sqlite3
import sys

db_path = sys.argv[1]
session_id = sys.argv[2]
limit = int(sys.argv[3])

conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row


def table_exists(name: str) -> bool:
    row = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    return row is not None


def columns(name: str) -> set[str]:
    if not table_exists(name):
        return set()
    return {row[1] for row in conn.execute(f"PRAGMA table_info({name})")}


def rows(sql: str, params=()) -> list[dict]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def one(sql: str, params=()) -> dict | None:
    row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def runtime_observation_rows(limit: int) -> list[dict]:
    if not table_exists("session_observations"):
        return []
    records = rows(
        """
        SELECT id, source, observed_at, received_at, payload_json
        FROM session_observations
        WHERE session_id=? AND source_domain='runtime'
        ORDER BY id DESC
        LIMIT ?
        """,
        (session_id, limit),
    )
    normalized = []
    for record in records:
        payload = json.loads(record.pop("payload_json") or "{}")
        runtime_payload = payload.get("payload") if isinstance(payload, dict) else {}
        if not isinstance(runtime_payload, dict):
            runtime_payload = {}
        normalized.append(
            {
                **record,
                "kind": payload.get("kind") if isinstance(payload, dict) else None,
                "phase": payload.get("phase") if isinstance(payload, dict) else None,
                "tool_name": payload.get("tool_name") if isinstance(payload, dict) else None,
                "freshness_ms": payload.get("freshness_ms") if isinstance(payload, dict) else None,
                "terminal_state": runtime_payload.get("terminal_state"),
                "terminal_reason": runtime_payload.get("terminal_reason"),
                "terminal_source": runtime_payload.get("terminal_source"),
                "payload": runtime_payload,
            }
        )
    return normalized


payload: dict[str, object] = {
    "db_path": db_path,
    "session_id": session_id,
    "tables": {
        "sessions": table_exists("sessions"),
        "events": table_exists("events"),
        "session_observations": table_exists("session_observations"),
        "session_runtime_state": table_exists("session_runtime_state"),
        "session_turns": table_exists("session_turns"),
    },
}

if table_exists("sessions"):
    wanted = [
        "id",
        "provider",
        "environment",
        "project",
        "device_id",
        "cwd",
        "git_branch",
        "started_at",
        "ended_at",
        "last_activity_at",
        "user_messages",
        "assistant_messages",
        "tool_calls",
        "provider_session_id",
        "summary_title",
        "execution_home",
        "managed_transport",
        "source_runner_name",
        "managed_session_name",
        "transcript_revision",
        "summary_revision",
        "embedding_revision",
    ]
    available = [name for name in wanted if name in columns("sessions")]
    payload["session"] = one(
        f"SELECT {', '.join(available)} FROM sessions WHERE id=? OR provider_session_id=?",
        (session_id, session_id),
    )
else:
    payload["session"] = None

if table_exists("session_runtime_state"):
    payload["runtime_state"] = one(
        "SELECT * FROM session_runtime_state WHERE session_id=? ORDER BY updated_at DESC LIMIT 1",
        (session_id,),
    )
else:
    payload["runtime_state"] = None

if table_exists("events"):
    payload["event_stats"] = one(
        """
        SELECT
            count(*) AS count,
            min(timestamp) AS first_timestamp,
            max(timestamp) AS last_timestamp,
            sum(CASE WHEN role='assistant' THEN 1 ELSE 0 END) AS assistant_events,
            sum(CASE WHEN role='tool' THEN 1 ELSE 0 END) AS tool_events,
            sum(CASE WHEN tool_name IS NOT NULL AND tool_name != '' THEN 1 ELSE 0 END) AS tool_call_events
        FROM events
        WHERE session_id=?
        """,
        (session_id,),
    )
    payload["recent_events"] = rows(
        """
        SELECT id, role, tool_name, substr(coalesce(content_text, tool_output_text, ''), 1, 180) AS text, timestamp
        FROM events
        WHERE session_id=?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (session_id, limit),
    )
else:
    payload["event_stats"] = None
    payload["recent_events"] = []

if table_exists("session_observations"):
    payload["runtime_observation_stats"] = one(
        """
        SELECT
            count(*) AS count,
            min(observed_at) AS first_observed_at,
            max(observed_at) AS last_observed_at,
            min(received_at) AS first_received_at,
            max(received_at) AS last_received_at
        FROM session_observations
        WHERE session_id=? AND source_domain='runtime'
        """,
        (session_id,),
    )
    payload["recent_runtime_observations"] = runtime_observation_rows(limit)
else:
    payload["runtime_observation_stats"] = None
    payload["recent_runtime_observations"] = []

if table_exists("session_turns"):
    payload["recent_turns"] = rows(
        """
        SELECT state, timing_confidence, terminal_phase, user_submitted_at, send_accepted_at,
               active_phase_observed_at, terminal_at, durable_at, created_at, updated_at
        FROM session_turns
        WHERE session_id=?
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (session_id, limit),
    )
else:
    payload["recent_turns"] = []

json.dump(payload, sys.stdout, default=str)
PY

RAW_LOGS_FILE="$(mktemp)"
ssh "$SSH_TARGET" "docker logs --since '$LOGS_SINCE' '$CONTAINER_NAME' 2>&1 || true" > "$RAW_LOGS_FILE"

COUNTS_FILE="$(mktemp)"
python3 - "$RAW_LOGS_FILE" > "$COUNTS_FILE" <<'PY'
import json
import pathlib
import re
import sys

text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
payload = {
    "agents_ingest": text.count("/api/agents/ingest"),
    "runtime_ingest_batches": text.count("/api/agents/runtime/events/batch"),
    "agents_presence": text.count("/api/agents/presence"),
    "telemetry_canary_observation": text.count("/api/telemetry/canary-observation"),
    "write_serializer_warnings": text.count("WriteSerializer:"),
}

waits = []
execs = []
for match in re.finditer(r"WriteSerializer: .*? waited ([0-9.]+)ms .*? exec ([0-9.]+)ms", text):
    waits.append(float(match.group(1)))
    execs.append(float(match.group(2)))

if waits:
    payload["write_serializer"] = {
        "count": len(waits),
        "avg_wait_ms": round(sum(waits) / len(waits), 1),
        "max_wait_ms": round(max(waits), 1),
        "avg_exec_ms": round(sum(execs) / len(execs), 1),
        "max_exec_ms": round(max(execs), 1),
    }
else:
    payload["write_serializer"] = None

json.dump(payload, sys.stdout)
PY

if [[ "$SHOW_LOGS" == "true" ]]; then
  LOGS_FILE="$(mktemp)"
  ssh "$SSH_TARGET" \
    "docker logs --since '$LOGS_SINCE' '$CONTAINER_NAME' 2>&1 | grep -F '$SESSION_ID' | tail -n 80 || true" \
    > "$LOGS_FILE"
fi

if [[ "$OUTPUT_MODE" == "json" ]]; then
  python3 - "$INSTANCE_SUBDOMAIN" "$LH_INSTANCE_ID" "$INSTANCE_URL" "${LH_INSTANCE_STATUS:-unknown}" "$CONTAINER_NAME" "$HOST_DATA_PATH" "$SQLITE_FILE" "$COUNTS_FILE" "${LOGS_FILE:-}" <<'PY'
import json
import pathlib
import sys

subdomain, instance_id, url, status, container_name, host_data_path, sqlite_file, counts_file, logs_file = sys.argv[1:]


def load_json(path: str, default):
    if not path:
        return default
    text = pathlib.Path(path).read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def load_lines(path: str) -> list[str]:
    if not path:
        return []
    return [line.rstrip("\n") for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


payload = {
    "instance": {
        "subdomain": subdomain,
        "instance_id": int(instance_id),
        "url": url,
        "status": status,
        "container": container_name,
        "host_data_path": host_data_path,
        "host_db_path": f"{host_data_path.rstrip('/')}/longhouse.db",
        "container_db_path": "/data/longhouse.db",
    },
    "database": load_json(sqlite_file, {}),
    "log_counts": load_json(counts_file, {}),
    "logs": load_lines(logs_file),
}
json.dump(payload, sys.stdout, indent=2, sort_keys=True)
sys.stdout.write("\n")
PY
  exit 0
fi

print_header "Instance"
cat <<EOF
subdomain: $INSTANCE_SUBDOMAIN
instance_id: ${LH_INSTANCE_ID}
url: ${INSTANCE_URL}
status: ${LH_INSTANCE_STATUS:-unknown}
container: ${CONTAINER_NAME}
host_data_path: ${HOST_DATA_PATH}
host_db_path: ${HOST_DB_PATH}
container_db_path: /data/longhouse.db
EOF

python3 - "$SQLITE_FILE" "$COUNTS_FILE" "${LOGS_FILE:-}" <<'PY'
import json
import pathlib
import sys

sqlite_payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
counts_payload = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
logs_file = sys.argv[3] if len(sys.argv) > 3 else ""


def header(title: str) -> None:
    print(f"\n== {title} ==")


def compact(row: dict | None, keys: list[str]) -> None:
    if not row:
        print("none")
        return
    for key in keys:
        if key in row:
            print(f"{key}: {row.get(key)}")


header("Session")
compact(
    sqlite_payload.get("session"),
    [
        "id",
        "provider",
        "project",
        "device_id",
        "cwd",
        "started_at",
        "ended_at",
        "last_activity_at",
        "execution_home",
        "managed_transport",
        "source_runner_name",
        "transcript_revision",
        "summary_revision",
        "embedding_revision",
    ],
)

header("Runtime State")
compact(
    sqlite_payload.get("runtime_state"),
    [
        "runtime_key",
        "phase",
        "phase_source",
        "active_tool",
        "last_runtime_signal_at",
        "last_progress_at",
        "last_live_at",
        "freshness_expires_at",
        "terminal_state",
        "terminal_at",
        "runtime_version",
        "updated_at",
    ],
)

header("Event Stats")
compact(
    sqlite_payload.get("event_stats"),
    ["count", "first_timestamp", "last_timestamp", "assistant_events", "tool_events", "tool_call_events"],
)

header("Runtime Observation Stats")
compact(
    sqlite_payload.get("runtime_observation_stats"),
    ["count", "first_observed_at", "last_observed_at", "first_received_at", "last_received_at"],
)

header("Recent Runtime Observations")
for row in sqlite_payload.get("recent_runtime_observations", []):
    print(
        f"{row.get('observed_at')} {row.get('kind')} phase={row.get('phase') or ''} "
        f"tool={row.get('tool_name') or ''} "
        f"terminal={row.get('terminal_state') or ''}/{row.get('terminal_reason') or ''}/{row.get('terminal_source') or ''} "
        f"received={row.get('received_at')} freshness_ms={row.get('freshness_ms') or ''}"
    )

header("Recent Events")
for row in sqlite_payload.get("recent_events", []):
    text = (row.get("text") or "").replace("\n", " ")
    print(f"{row.get('timestamp')} #{row.get('id')} {row.get('role')} tool={row.get('tool_name') or ''} {text}")

header("Recent Turns")
for row in sqlite_payload.get("recent_turns", []):
    print(
        f"{row.get('created_at')} state={row.get('state')} confidence={row.get('timing_confidence')} "
        f"terminal={row.get('terminal_phase') or ''} durable_at={row.get('durable_at') or ''}"
    )

header("Log Counts")
for key, value in counts_payload.items():
    print(f"{key}: {value}")

if logs_file:
    header("Session Logs")
    lines = [line.rstrip("\n") for line in pathlib.Path(logs_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        print("none")
    else:
        for line in lines:
            print(line)
PY
