#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-run}"
ENV_FILE="${ZERG_OPS_ENV_FILE:-/etc/zerg-ops.env}"

load_env_defaults() {
  local file="$1"
  [[ -f "$file" ]] || return 0

  local line key value
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ "$line" =~ ^[[:space:]]*# ]] && continue
    [[ -z "${line//[[:space:]]/}" ]] && continue
    [[ "$line" == *"="* ]] || continue

    key="${line%%=*}"
    value="${line#*=}"
    key="${key#"${key%%[![:space:]]*}"}"
    key="${key%"${key##*[![:space:]]}"}"
    value="${value#"${value%%[![:space:]]*}"}"
    value="${value%"${value##*[![:space:]]}"}"

    if [[ "$value" == \"*\" && "$value" == *\" ]]; then
      value="${value:1:${#value}-2}"
    fi
    if [[ "$value" == \'*\' && "$value" == *\' ]]; then
      value="${value:1:${#value}-2}"
    fi

    [[ -n "$key" ]] || continue
    if [[ -z "${!key+x}" ]]; then
      export "$key=$value"
    fi
  done < "$file"
}

load_env_defaults "$ENV_FILE"

BACKUP_ROOT="${BACKUP_ROOT:-/var/app-data/longhouse-backups}"
LIVE_ROOT="${LIVE_ROOT:-/var/lib/docker/data/longhouse}"
TMP_BACKUP_DIR="${TMP_BACKUP_DIR:-$BACKUP_ROOT/tmp}"
INSTANCE_ALLOWLIST="${INSTANCE_ALLOWLIST:-}"
DISCOVERY_MODE="${DISCOVERY_MODE:-running}"
KEEP_SNAPSHOTS="${KEEP_SNAPSHOTS:-14}"
KEEP_DAYS_PRE="${KEEP_DAYS_PRE:-14}"
KEEP_DAYS_TMP="${KEEP_DAYS_TMP:-3}"
VERIFY_ON_BACKUP="${VERIFY_ON_BACKUP:-true}"
ENABLE_DOCKER_PRUNE="${ENABLE_DOCKER_PRUNE:-true}"
ROOT_WARN_PCT="${ROOT_WARN_PCT:-85}"
DOCKER_PRUNE_UNTIL_HOURS="${DOCKER_PRUNE_UNTIL_HOURS:-240}"
REMOTE_SSH_TARGET="${REMOTE_SSH_TARGET:-}"
REMOTE_BASE_PATH="${REMOTE_BASE_PATH:-}"
REMOTE_KEEP_SNAPSHOTS="${REMOTE_KEEP_SNAPSHOTS:-30}"
MONITOR_MAX_AGE_HOURS="${MONITOR_MAX_AGE_HOURS:-30}"
MONITOR_REQUIRE_REMOTE="${MONITOR_REQUIRE_REMOTE:-auto}"
ALERT_WEBHOOK_URL="${ALERT_WEBHOOK_URL:-}"
LOCK_FILE="${LOCK_FILE:-/run/zerg-ops.lock}"

log() {
  echo "[zerg-ops $(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*"
}

ssh_cmd() {
  ssh -n "$@"
}

send_alert_webhook() {
  local message="$1"
  [[ -n "$ALERT_WEBHOOK_URL" ]] || return 0

  local payload
  payload="$(
    python3 - "$message" <<'PY'
import json
import socket
import sys

msg = sys.argv[1]
host = socket.gethostname()
body = f"[zerg-ops monitor] {host}: {msg}"
print(json.dumps({"text": body, "content": body}, separators=(",", ":")))
PY
  )"

  if ! curl -fsS -m 15 -H "Content-Type: application/json" -d "$payload" "$ALERT_WEBHOOK_URL" >/dev/null; then
    log "WARNING alert webhook delivery failed"
  fi
}

die() {
  log "ERROR: $*"
  exit 1
}

is_true() {
  case "${1:-}" in
    1|true|TRUE|yes|YES|on|ON) return 0 ;;
    *) return 1 ;;
  esac
}

trim() {
  local value="${1:-}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf "%s" "$value"
}

stat_bytes() {
  local path="$1"
  if stat -f%z "$path" >/dev/null 2>&1; then
    stat -f%z "$path"
  else
    stat -c%s "$path"
  fi
}

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
  else
    shasum -a 256 "$path" | awk '{print $1}'
  fi
}

archive_ext() {
  if command -v zstd >/dev/null 2>&1; then
    echo "zst"
  else
    echo "gz"
  fi
}

compress_file() {
  local src="$1"
  local dst="$2"
  if [[ "$dst" == *.zst ]]; then
    zstd -q -T0 -3 -c "$src" > "$dst"
  elif [[ "$dst" == *.gz ]]; then
    gzip -c "$src" > "$dst"
  else
    die "unknown archive extension for $dst"
  fi
}

decompress_file() {
  local src="$1"
  local dst="$2"
  if [[ "$src" == *.zst ]]; then
    zstd -q -dc "$src" > "$dst"
  elif [[ "$src" == *.gz ]]; then
    gzip -dc "$src" > "$dst"
  else
    die "unknown archive extension for $src"
  fi
}

discover_allowlist_instances() {
  local raw="${INSTANCE_ALLOWLIST//,/ }"
  local inst
  for inst in $raw; do
    inst="$(trim "$inst")"
    [[ -n "$inst" ]] && echo "$inst"
  done
}

discover_live_instances() {
  if [[ -n "$INSTANCE_ALLOWLIST" ]]; then
    discover_allowlist_instances | sort -u
    return
  fi

  if [[ "$DISCOVERY_MODE" == "running" ]] && command -v docker >/dev/null 2>&1; then
    local discovered=""
    local instance
    while IFS= read -r instance; do
      [[ -n "$instance" ]] || continue
      [[ -f "$LIVE_ROOT/$instance/longhouse.db" ]] || continue
      echo "$instance"
      discovered="1"
    done < <(docker ps --format '{{.Names}}' 2>/dev/null | sed -n 's/^longhouse-//p' | sort -u)
    if [[ -n "$discovered" ]]; then
      return
    fi
  fi

  if [[ ! -d "$LIVE_ROOT" ]]; then
    return
  fi

  local dir
  for dir in "$LIVE_ROOT"/*; do
    [[ -d "$dir" ]] || continue
    [[ -f "$dir/longhouse.db" ]] || continue
    basename "$dir"
  done | sort -u
}

discover_backup_instances() {
  if [[ -n "$INSTANCE_ALLOWLIST" ]]; then
    discover_allowlist_instances | sort -u
    return
  fi

  if [[ ! -d "$BACKUP_ROOT" ]]; then
    return
  fi

  local dir
  for dir in "$BACKUP_ROOT"/*; do
    [[ -d "$dir" ]] || continue
    [[ "$(basename "$dir")" == "tmp" ]] && continue
    ls "$dir"/longhouse.*.sqlite.* >/dev/null 2>&1 || continue
    basename "$dir"
  done | sort -u
}

sqlite_backup_copy() {
  local src_db="$1"
  local dst_db="$2"
  python3 - "$src_db" "$dst_db" <<'PY'
import sqlite3
import sys

src_path, dst_path = sys.argv[1], sys.argv[2]
src = sqlite3.connect(src_path)
try:
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)
    finally:
        dst.close()
finally:
    src.close()
PY
}

db_counts_json() {
  local db_path="$1"
  python3 - "$db_path" <<'PY'
import json
import sqlite3
import sys

db_path = sys.argv[1]
conn = sqlite3.connect(db_path)
cur = conn.cursor()

tables = {}
for table in ("sessions", "events", "session_presence", "agent_sessions", "agent_events"):
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    if cur.fetchone():
        cur.execute(f"SELECT COUNT(*) FROM {table}")
        tables[table] = int(cur.fetchone()[0])

conn.close()
print(json.dumps(tables, separators=(",", ":")))
PY
}

verify_restored_db() {
  local restored_db="$1"
  local expected_counts_json="$2"
  EXPECTED_COUNTS_JSON="$expected_counts_json" python3 - "$restored_db" <<'PY'
import json
import os
import sqlite3
import sys

db_path = sys.argv[1]
expected = json.loads(os.environ.get("EXPECTED_COUNTS_JSON", "{}"))

conn = sqlite3.connect(db_path)
cur = conn.cursor()

cur.execute("PRAGMA integrity_check")
integrity = cur.fetchone()[0]
if integrity != "ok":
    print(f"integrity_check failed: {integrity}")
    sys.exit(1)

for table, expected_count in expected.items():
    cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    )
    if not cur.fetchone():
        print(f"missing expected table: {table}")
        sys.exit(1)
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    actual = int(cur.fetchone()[0])
    if actual != int(expected_count):
        print(f"row-count mismatch for {table}: expected={expected_count} actual={actual}")
        sys.exit(1)

conn.close()
PY
}

manifest_write() {
  local manifest_path="$1"
  local instance="$2"
  local timestamp="$3"
  local source_db="$4"
  local source_db_bytes="$5"
  local snapshot_bytes="$6"
  local snapshot_sha="$7"
  local archive_name="$8"
  local archive_bytes="$9"
  local counts_json="${10}"
  local verified="${11}"

  python3 - "$manifest_path" "$instance" "$timestamp" "$source_db" "$source_db_bytes" "$snapshot_bytes" "$snapshot_sha" "$archive_name" "$archive_bytes" "$counts_json" "$verified" <<'PY'
import json
import sys

(
    manifest_path,
    instance,
    timestamp,
    source_db,
    source_db_bytes,
    snapshot_bytes,
    snapshot_sha256,
    archive_name,
    archive_bytes,
    counts_json,
    verified,
) = sys.argv[1:12]

manifest = {
    "instance": instance,
    "timestamp_utc": timestamp,
    "source_db": source_db,
    "source_db_bytes": int(source_db_bytes),
    "snapshot_bytes": int(snapshot_bytes),
    "snapshot_sha256": snapshot_sha256,
    "archive_name": archive_name,
    "archive_bytes": int(archive_bytes),
    "counts": json.loads(counts_json),
    "verified_restore": verified == "true",
}

with open(manifest_path, "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2, sort_keys=True)
    f.write("\n")
PY
}

remote_enabled() {
  [[ -n "$REMOTE_SSH_TARGET" && -n "$REMOTE_BASE_PATH" ]]
}

monitor_remote_required() {
  case "${MONITOR_REQUIRE_REMOTE:-auto}" in
    auto|AUTO|"") remote_enabled ;;
    *) is_true "$MONITOR_REQUIRE_REMOTE" ;;
  esac
}

remote_prune_instance() {
  local instance="$1"
  local remote_dir="${REMOTE_BASE_PATH%/}/$instance"
  local stale
  stale="$(
    ssh_cmd "$REMOTE_SSH_TARGET" \
      "cd '$remote_dir' 2>/dev/null || exit 0; ls -1t longhouse.*.sqlite.* 2>/dev/null | awk 'NR>${REMOTE_KEEP_SNAPSHOTS}'" \
      || true
  )"

  local file ts
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    ts="${file#longhouse.}"
    ts="${ts%.sqlite.*}"
    ssh_cmd "$REMOTE_SSH_TARGET" \
      "rm -f '$remote_dir/$file' '$remote_dir/longhouse.$ts.manifest.json'" \
      || true
    log "remote pruned $instance/$file"
  done <<<"$stale"
}

remote_sync_snapshot() {
  local instance="$1"
  local archive_path="$2"
  local manifest_path="$3"

  remote_enabled || return 0

  local remote_dir="${REMOTE_BASE_PATH%/}/$instance"
  ssh_cmd "$REMOTE_SSH_TARGET" "mkdir -p '$remote_dir'"
  rsync -az "$archive_path" "$manifest_path" "$REMOTE_SSH_TARGET:$remote_dir/"
  log "remote sync complete for $instance -> ${REMOTE_SSH_TARGET}:$remote_dir"

  if [[ "$REMOTE_KEEP_SNAPSHOTS" =~ ^[0-9]+$ ]] && (( REMOTE_KEEP_SNAPSHOTS > 0 )); then
    remote_prune_instance "$instance"
  fi
}

snapshot_instance() {
  local instance="$1"
  local source_db="$LIVE_ROOT/$instance/longhouse.db"
  [[ -f "$source_db" ]] || { log "skip $instance (no longhouse.db at $source_db)"; return 0; }

  mkdir -p "$TMP_BACKUP_DIR" "$BACKUP_ROOT/$instance"

  local ts
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  local workdir
  workdir="$(mktemp -d "${TMP_BACKUP_DIR%/}/${instance}-${ts}.XXXXXX")"

  local raw_snapshot="$workdir/longhouse.$ts.sqlite"
  local ext
  ext="$(archive_ext)"
  local archive_tmp="$workdir/longhouse.$ts.sqlite.$ext"
  local restore_tmp="$workdir/restore.$ts.sqlite"

  log "snapshot start instance=$instance"
  sqlite_backup_copy "$source_db" "$raw_snapshot"

  local source_db_bytes snapshot_bytes snapshot_sha counts_json
  source_db_bytes="$(stat_bytes "$source_db")"
  snapshot_bytes="$(stat_bytes "$raw_snapshot")"
  snapshot_sha="$(sha256_file "$raw_snapshot")"
  counts_json="$(db_counts_json "$raw_snapshot")"

  compress_file "$raw_snapshot" "$archive_tmp"

  local verified="false"
  if is_true "$VERIFY_ON_BACKUP"; then
    decompress_file "$archive_tmp" "$restore_tmp"
    local restore_sha
    restore_sha="$(sha256_file "$restore_tmp")"
    [[ "$restore_sha" == "$snapshot_sha" ]] || die "hash mismatch after restore for $instance"
    verify_restored_db "$restore_tmp" "$counts_json"
    verified="true"
  fi

  local archive_name archive_final manifest_final archive_bytes
  archive_name="$(basename "$archive_tmp")"
  archive_final="$BACKUP_ROOT/$instance/$archive_name"
  manifest_final="$BACKUP_ROOT/$instance/longhouse.$ts.manifest.json"

  mv "$archive_tmp" "$archive_final"
  archive_bytes="$(stat_bytes "$archive_final")"
  manifest_write \
    "$manifest_final" \
    "$instance" \
    "$ts" \
    "$source_db" \
    "$source_db_bytes" \
    "$snapshot_bytes" \
    "$snapshot_sha" \
    "$archive_name" \
    "$archive_bytes" \
    "$counts_json" \
    "$verified"

  remote_sync_snapshot "$instance" "$archive_final" "$manifest_final"
  rm -rf "$workdir"
  log "snapshot done instance=$instance archive=$(basename "$archive_final") verified=$verified"
}

prune_instance_local() {
  local instance="$1"
  local instance_dir="$BACKUP_ROOT/$instance"
  [[ -d "$instance_dir" ]] || return 0

  local keep="$KEEP_SNAPSHOTS"
  [[ "$keep" =~ ^[0-9]+$ ]] || die "KEEP_SNAPSHOTS must be an integer"
  (( keep >= 1 )) || die "KEEP_SNAPSHOTS must be >= 1"

  local idx=0 file ts
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    idx=$((idx + 1))
    if (( idx <= keep )); then
      continue
    fi
    ts="${file##*/}"
    ts="${ts#longhouse.}"
    ts="${ts%.sqlite.*}"
    rm -f "$file" "$instance_dir/longhouse.$ts.manifest.json"
    log "local pruned $instance/$(basename "$file")"
  done < <(ls -1t "$instance_dir"/longhouse.*.sqlite.* 2>/dev/null || true)
}

prune_all_local() {
  local instance
  while IFS= read -r instance; do
    [[ -n "$instance" ]] || continue
    prune_instance_local "$instance"
  done < <(discover_backup_instances)
}

verify_latest_instance() {
  local instance="$1"
  local instance_dir="$BACKUP_ROOT/$instance"
  [[ -d "$instance_dir" ]] || { log "verify skip $instance (no backup dir)"; return 0; }

  local latest
  latest="$(ls -1t "$instance_dir"/longhouse.*.sqlite.* 2>/dev/null | head -n1 || true)"
  [[ -n "$latest" ]] || { log "verify skip $instance (no backup archives)"; return 0; }

  local ts manifest
  ts="$(basename "$latest")"
  ts="${ts#longhouse.}"
  ts="${ts%.sqlite.*}"
  manifest="$instance_dir/longhouse.$ts.manifest.json"
  [[ -f "$manifest" ]] || die "missing manifest for $latest"

  local expected_sha expected_counts restore_tmp actual_sha
  expected_sha="$(
    python3 - "$manifest" <<'PY'
import json
import sys
print(json.load(open(sys.argv[1], encoding="utf-8"))["snapshot_sha256"])
PY
  )"
  expected_counts="$(
    python3 - "$manifest" <<'PY'
import json
import sys
print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8"))["counts"], separators=(",", ":")))
PY
  )"

  restore_tmp="$(mktemp "${TMP_BACKUP_DIR%/}/verify-${instance}.XXXXXX.sqlite")"
  decompress_file "$latest" "$restore_tmp"
  actual_sha="$(sha256_file "$restore_tmp")"
  [[ "$actual_sha" == "$expected_sha" ]] || die "verify hash mismatch for $instance"
  verify_restored_db "$restore_tmp" "$expected_counts"
  rm -f "$restore_tmp"
  log "verify ok instance=$instance archive=$(basename "$latest")"
}

backup_all() {
  local count=0 instance
  while IFS= read -r instance; do
    [[ -n "$instance" ]] || continue
    count=$((count + 1))
    snapshot_instance "$instance"
  done < <(discover_live_instances)

  if (( count == 0 )); then
    log "no live instances discovered under $LIVE_ROOT"
  fi
}

verify_latest_all() {
  local count=0 instance
  while IFS= read -r instance; do
    [[ -n "$instance" ]] || continue
    count=$((count + 1))
    verify_latest_instance "$instance"
  done < <(discover_backup_instances)

  if (( count == 0 )); then
    log "no backup instances discovered under $BACKUP_ROOT"
  fi
}

hours_since_timestamp() {
  local timestamp_utc="$1"
  python3 - "$timestamp_utc" <<'PY'
import datetime
import sys

ts = sys.argv[1]
dt = datetime.datetime.strptime(ts, "%Y%m%dT%H%M%SZ").replace(
    tzinfo=datetime.timezone.utc
)
now = datetime.datetime.now(datetime.timezone.utc)
print(int((now - dt).total_seconds() // 3600))
PY
}

manifest_get_archive_name() {
  local manifest_path="$1"
  python3 - "$manifest_path" <<'PY'
import json
import sys

manifest = json.load(open(sys.argv[1], encoding="utf-8"))
print(manifest["archive_name"])
PY
}

monitor_instance() {
  local instance="$1"
  local instance_dir="$BACKUP_ROOT/$instance"
  local latest_archive ts manifest age_hours

  latest_archive="$(ls -1t "$instance_dir"/longhouse.*.sqlite.* 2>/dev/null | head -n1 || true)"
  if [[ -z "$latest_archive" ]]; then
    log "ERROR monitor instance=$instance no local archive found"
    return 1
  fi

  ts="$(basename "$latest_archive")"
  ts="${ts#longhouse.}"
  ts="${ts%.sqlite.*}"
  manifest="$instance_dir/longhouse.$ts.manifest.json"
  if [[ ! -f "$manifest" ]]; then
    log "ERROR monitor instance=$instance missing local manifest longhouse.$ts.manifest.json"
    return 1
  fi

  age_hours="$(hours_since_timestamp "$ts")"
  if (( age_hours > MONITOR_MAX_AGE_HOURS )); then
    log "ERROR monitor instance=$instance backup_age_hours=$age_hours threshold=$MONITOR_MAX_AGE_HOURS"
    return 1
  fi

  if monitor_remote_required; then
    if ! remote_enabled; then
      log "ERROR monitor instance=$instance remote required but REMOTE_* not configured"
      return 1
    fi

    local archive_name local_archive local_size remote_dir remote_archive remote_manifest remote_size
    archive_name="$(manifest_get_archive_name "$manifest")"
    local_archive="$instance_dir/$archive_name"
    if [[ ! -f "$local_archive" ]]; then
      log "ERROR monitor instance=$instance local archive missing $archive_name"
      return 1
    fi

    remote_dir="${REMOTE_BASE_PATH%/}/$instance"
    remote_archive="$remote_dir/$archive_name"
    remote_manifest="$remote_dir/longhouse.$ts.manifest.json"

    if ! ssh_cmd "$REMOTE_SSH_TARGET" "test -f '$remote_archive' && test -f '$remote_manifest'"; then
      log "ERROR monitor instance=$instance remote archive/manifest missing for ts=$ts"
      return 1
    fi

    local_size="$(stat_bytes "$local_archive")"
    remote_size="$(
      ssh_cmd "$REMOTE_SSH_TARGET" \
        "if stat -c%s '$remote_archive' >/dev/null 2>&1; then stat -c%s '$remote_archive'; else stat -f%z '$remote_archive'; fi"
    )"

    if [[ "$local_size" != "$remote_size" ]]; then
      log "ERROR monitor instance=$instance remote_size_mismatch local=$local_size remote=$remote_size archive=$archive_name"
      return 1
    fi
  fi

  log "monitor ok instance=$instance age_hours=$age_hours archive=$(basename "$latest_archive")"
  return 0
}

monitor_all() {
  local fail=0 count=0 instance

  discover_monitor_instances() {
    if [[ -n "$INSTANCE_ALLOWLIST" ]]; then
      discover_allowlist_instances | sort -u
      return
    fi
    {
      discover_live_instances
      discover_backup_instances
    } | awk 'NF > 0' | sort -u
  }

  while IFS= read -r instance; do
    [[ -n "$instance" ]] || continue
    count=$((count + 1))
    if ! monitor_instance "$instance"; then
      fail=$((fail + 1))
    fi
  done < <(discover_monitor_instances)

  if (( count == 0 )); then
    local msg="monitor found no instances to check (live or backup)"
    log "ERROR $msg"
    send_alert_webhook "$msg"
    return 1
  fi

  if (( fail > 0 )); then
    local msg="monitor failures=$fail checked_instances=$count"
    log "ERROR $msg"
    send_alert_webhook "$msg"
    return 1
  fi

  log "monitor success checked_instances=$count"
  return 0
}

cleanup_legacy_artifacts() {
  mkdir -p "$BACKUP_ROOT" "$TMP_BACKUP_DIR"

  local file inst dest_dir base dest ts
  while IFS= read -r -d '' file; do
    inst="$(basename "$(dirname "$file")")"
    dest_dir="$BACKUP_ROOT/$inst"
    mkdir -p "$dest_dir"
    base="$(basename "$file")"
    dest="$dest_dir/$base"
    if [[ -e "$dest" ]]; then
      ts="$(date -u +%Y%m%dT%H%M%SZ)"
      dest="$dest_dir/${base%.db}-$ts.db"
    fi
    mv "$file" "$dest"
    log "moved legacy snapshot $file -> $dest"
  done < <(find "$LIVE_ROOT" -maxdepth 2 -type f -name "longhouse.pre-*.db" -print0 2>/dev/null)

  while IFS= read -r -d '' file; do
    base="$(basename "$file")"
    dest="$TMP_BACKUP_DIR/$base"
    if [[ -e "$dest" ]]; then
      ts="$(date -u +%Y%m%dT%H%M%SZ)"
      dest="$TMP_BACKUP_DIR/${base%.db}-$ts.db"
    fi
    mv "$file" "$dest"
    log "moved temp artifact $file -> $dest"
  done < <(find /tmp -maxdepth 1 -type f \( -name "longhouse*.db" -o -name "agents*.db" \) -print0 2>/dev/null)

  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    rm -f "$file"
    log "deleted old legacy snapshot $file"
  done < <(find "$BACKUP_ROOT" -type f -name "longhouse.pre-*.db" -mtime +"$KEEP_DAYS_PRE" -print 2>/dev/null)

  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    rm -f "$file"
    log "deleted old tmp artifact $file"
  done < <(find "$TMP_BACKUP_DIR" -type f -mtime +"$KEEP_DAYS_TMP" -print 2>/dev/null)
}

cleanup_docker() {
  if ! is_true "$ENABLE_DOCKER_PRUNE"; then
    log "docker prune disabled"
    return
  fi

  docker builder prune -af --filter "until=${DOCKER_PRUNE_UNTIL_HOURS}h" >/dev/null 2>&1 || true
  docker image prune -f >/dev/null 2>&1 || true
  docker volume prune -f >/dev/null 2>&1 || true
  log "docker prune complete"
}

report() {
  log "report start"

  if [[ -d /var/app-data ]]; then
    df -h / /var/app-data
  else
    df -h /
  fi
  echo

  if [[ -d /var/lib/docker ]]; then
    du -xh --max-depth=1 /var/lib/docker 2>/dev/null | sort -h | tail -n 20
    echo
  fi

  local instance_dir instance latest size_bytes count
  for instance_dir in "$BACKUP_ROOT"/*; do
    [[ -d "$instance_dir" ]] || continue
    instance="$(basename "$instance_dir")"
    [[ "$instance" == "tmp" ]] && continue
    latest="$(ls -1t "$instance_dir"/longhouse.*.sqlite.* 2>/dev/null | head -n1 || true)"
    count="$(ls -1 "$instance_dir"/longhouse.*.sqlite.* 2>/dev/null | wc -l | tr -d ' ')"
    if [[ -n "$latest" ]]; then
      size_bytes="$(stat_bytes "$latest")"
      log "backup instance=$instance files=$count latest=$(basename "$latest") latest_bytes=$size_bytes"
    else
      log "backup instance=$instance files=0"
    fi
  done

  local root_use
  root_use="$(df --output=pcent / | tail -1 | tr -dc '0-9')"
  if (( root_use >= ROOT_WARN_PCT )); then
    log "WARNING root disk usage ${root_use}% (>= ${ROOT_WARN_PCT}%)"
  else
    log "root disk usage ${root_use}%"
  fi
}

usage() {
  cat <<USAGE
Usage:
  zerg-ops run      Cleanup + backup + verify + prune + report
  zerg-ops backup   Backup + optional verify + prune
  zerg-ops verify   Verify latest archive per instance
  zerg-ops monitor  Backup freshness + offsite presence/size check
  zerg-ops cleanup  Legacy cleanup + prune + docker cleanup + report
  zerg-ops report   Print disk + backup summary
USAGE
}

main() {
  mkdir -p "$BACKUP_ROOT" "$TMP_BACKUP_DIR"
  if [[ ! -d "$(dirname "$LOCK_FILE")" ]]; then
    LOCK_FILE="/tmp/zerg-ops.lock"
  fi
  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    flock -n 9 || { log "another zerg-ops run is active; exiting"; exit 0; }
  else
    local lock_dir="${LOCK_FILE}.d"
    if ! mkdir "$lock_dir" 2>/dev/null; then
      log "another zerg-ops run is active; exiting"
      exit 0
    fi
    trap 'rmdir "'"$lock_dir"'" 2>/dev/null || true' EXIT
  fi

  case "$MODE" in
    run)
      cleanup_legacy_artifacts
      backup_all
      verify_latest_all
      prune_all_local
      cleanup_docker
      report
      ;;
    backup)
      backup_all
      if is_true "$VERIFY_ON_BACKUP"; then
        verify_latest_all
      fi
      prune_all_local
      ;;
    verify)
      verify_latest_all
      ;;
    monitor)
      monitor_all
      ;;
    cleanup)
      cleanup_legacy_artifacts
      prune_all_local
      cleanup_docker
      report
      ;;
    report)
      report
      ;;
    *)
      usage
      exit 2
      ;;
  esac
}

main
