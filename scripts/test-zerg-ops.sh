#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OPS_SCRIPT="$ROOT_DIR/scripts/zerg-ops.sh"

fail() {
  echo "FAIL: $*" >&2
  exit 1
}

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  else
    shasum -a 256 "$path" | awk '{print $1}'
  fi
}

create_db() {
  local db_path="$1"
  local session_rows="$2"
  local event_rows="$3"
  python3 - "$db_path" "$session_rows" "$event_rows" <<'PY'
import sqlite3
import sys

db_path, session_rows, event_rows = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
conn = sqlite3.connect(db_path)
cur = conn.cursor()
cur.execute("CREATE TABLE IF NOT EXISTS sessions (id INTEGER PRIMARY KEY, provider TEXT)")
cur.execute("CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, role TEXT, content_text TEXT)")
cur.execute("DELETE FROM sessions")
cur.execute("DELETE FROM events")
for i in range(session_rows):
    cur.execute("INSERT INTO sessions(provider) VALUES (?)", (f'p-{i}',))
for i in range(event_rows):
    cur.execute("INSERT INTO events(role, content_text) VALUES (?, ?)", ("user", f"evt-{i}"))
conn.commit()
conn.close()
PY
}

main() {
  [[ -f "$OPS_SCRIPT" ]] || fail "missing script: $OPS_SCRIPT"

  local workdir
  workdir="$(mktemp -d)"
  trap 'if [[ -n "${workdir:-}" ]]; then rm -rf "$workdir"; fi' EXIT

  local live_root="$workdir/live"
  local backup_root="$workdir/backups"
  local tmp_root="$workdir/tmp"
  mkdir -p "$live_root/alice" "$live_root/bob" "$backup_root" "$tmp_root"

  create_db "$live_root/alice/longhouse.db" 2 5
  create_db "$live_root/bob/longhouse.db" 1 3

  export LIVE_ROOT="$live_root"
  export BACKUP_ROOT="$backup_root"
  export TMP_BACKUP_DIR="$tmp_root"
  export KEEP_SNAPSHOTS=2
  export VERIFY_ON_BACKUP=true
  export ENABLE_DOCKER_PRUNE=false
  export ROOT_WARN_PCT=100
  export INSTANCE_ALLOWLIST=
  export REMOTE_SSH_TARGET=
  export REMOTE_BASE_PATH=
  export REMOTE_KEEP_SNAPSHOTS=30
  export MONITOR_MAX_AGE_HOURS=30
  export MONITOR_REQUIRE_REMOTE=false

  local round
  for round in 1 2 3; do
    create_db "$live_root/alice/longhouse.db" "$((2 + round))" "$((5 + round))"
    create_db "$live_root/bob/longhouse.db" "$((1 + round))" "$((3 + round))"
    bash "$OPS_SCRIPT" backup >/dev/null
    sleep 1
  done

  bash "$OPS_SCRIPT" verify >/dev/null
  bash "$OPS_SCRIPT" monitor >/dev/null

  local instance count manifests latest ts manifest restore expected_sha actual_sha
  for instance in alice bob; do
    count="$(ls -1 "$backup_root/$instance"/longhouse.*.sqlite.* 2>/dev/null | wc -l | tr -d ' ')"
    manifests="$(ls -1 "$backup_root/$instance"/longhouse.*.manifest.json 2>/dev/null | wc -l | tr -d ' ')"
    [[ "$count" == "2" ]] || fail "expected 2 archives for $instance, got $count"
    [[ "$manifests" == "$count" ]] || fail "manifest/archive count mismatch for $instance"

    latest="$(ls -1t "$backup_root/$instance"/longhouse.*.sqlite.* | head -n1)"
    ts="$(basename "$latest")"
    ts="${ts#longhouse.}"
    ts="${ts%.sqlite.*}"
    manifest="$backup_root/$instance/longhouse.$ts.manifest.json"
    [[ -f "$manifest" ]] || fail "missing manifest for latest $instance archive"

    restore="$tmp_root/restore-${instance}.sqlite"
    if [[ "$latest" == *.zst ]]; then
      zstd -q -dc "$latest" > "$restore"
    else
      gzip -dc "$latest" > "$restore"
    fi

    expected_sha="$(
      python3 - "$manifest" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["snapshot_sha256"])
PY
    )"
    actual_sha="$(sha256_file "$restore")"
    [[ "$actual_sha" == "$expected_sha" ]] || fail "sha mismatch for $instance latest archive"

    python3 - "$restore" "$manifest" <<'PY'
import json
import sqlite3
import sys

db_path, manifest_path = sys.argv[1], sys.argv[2]
manifest = json.load(open(manifest_path, encoding="utf-8"))
expected = manifest["counts"]

conn = sqlite3.connect(db_path)
cur = conn.cursor()
cur.execute("PRAGMA integrity_check")
if cur.fetchone()[0] != "ok":
    raise SystemExit("integrity_check failed")

for table, expected_count in expected.items():
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    actual = int(cur.fetchone()[0])
    if actual != int(expected_count):
        raise SystemExit(f"{table} mismatch: expected {expected_count}, got {actual}")

conn.close()
PY
  done

  echo "PASS: zerg-ops backup/restore retention contract"
}

main "$@"
