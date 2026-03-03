#!/usr/bin/env bash
set -euo pipefail

MODE="run"

# Opinionated zerg defaults live in code on purpose.
LIVE_ROOT="/var/lib/docker/data/longhouse"
BACKUP_ROOT="/var/app-data/longhouse-backups"
TMP_BACKUP_DIR="$BACKUP_ROOT/tmp"
KEEP_SNAPSHOTS=14
VERIFY_ON_BACKUP="true"
MONITOR_MAX_AGE_HOURS=30

# Offsite sync uses a neutral SSH alias. Map this alias in host ssh config.
OFFSITE_ENABLED="true"
OFFSITE_SSH_TARGET="longhouse-offsite"
OFFSITE_BASE_PATH="longhouse-backups"
OFFSITE_KEEP_SNAPSHOTS=30

ENABLE_DOCKER_PRUNE="true"
DOCKER_PRUNE_UNTIL_HOURS=240
ROOT_WARN_PCT=85
LOCK_FILE="/run/zerg-ops.lock"

TARGET_INSTANCES=()

log() {
  echo "[zerg-ops $(date -u +"%Y-%m-%dT%H:%M:%SZ")] $*"
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

usage() {
  cat <<'USAGE'
Usage:
  zerg-ops run
  zerg-ops backup
  zerg-ops verify
  zerg-ops monitor
  zerg-ops cleanup
  zerg-ops report

Options:
  --instance <name>       Scope run to one instance (repeatable)
  --live-root <path>      Override live DB root (testing)
  --backup-root <path>    Override backup root (testing)
  --tmp-backup-dir <path> Override temporary working directory (testing)
  --no-offsite            Disable offsite sync/check for this invocation
  --no-docker-prune       Disable docker prune for this invocation
  -h, --help              Show help
USAGE
}

parse_args() {
  if (($# > 0)) && [[ "${1:-}" != --* ]]; then
    MODE="$1"
    shift
  fi

  while (($# > 0)); do
    case "$1" in
      --instance)
        [[ -n "${2:-}" ]] || die "--instance requires a value"
        TARGET_INSTANCES+=("$2")
        shift 2
        ;;
      --live-root)
        [[ -n "${2:-}" ]] || die "--live-root requires a value"
        LIVE_ROOT="$2"
        shift 2
        ;;
      --backup-root)
        [[ -n "${2:-}" ]] || die "--backup-root requires a value"
        BACKUP_ROOT="$2"
        TMP_BACKUP_DIR="$BACKUP_ROOT/tmp"
        shift 2
        ;;
      --tmp-backup-dir)
        [[ -n "${2:-}" ]] || die "--tmp-backup-dir requires a value"
        TMP_BACKUP_DIR="$2"
        shift 2
        ;;
      --no-offsite)
        OFFSITE_ENABLED="false"
        shift
        ;;
      --no-docker-prune)
        ENABLE_DOCKER_PRUNE="false"
        shift
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        die "unknown argument: $1"
        ;;
    esac
  done
}

trim() {
  local value="${1:-}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf "%s" "$value"
}

ssh_cmd() {
  ssh -n "$@"
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

discover_instances_from_targets() {
  if ((${#TARGET_INSTANCES[@]} == 0)); then
    return
  fi

  local inst
  for inst in "${TARGET_INSTANCES[@]}"; do
    inst="$(trim "$inst")"
    [[ -n "$inst" ]] && echo "$inst"
  done | sort -u
}

discover_live_instances() {
  if ((${#TARGET_INSTANCES[@]} > 0)); then
    discover_instances_from_targets
    return
  fi

  if command -v docker >/dev/null 2>&1; then
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
  if ((${#TARGET_INSTANCES[@]} > 0)); then
    discover_instances_from_targets
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

offsite_enabled() {
  is_true "$OFFSITE_ENABLED"
}

remote_sync_snapshot() {
  local instance="$1"
  local archive_path="$2"
  local manifest_path="$3"

  offsite_enabled || return 0

  local remote_dir="${OFFSITE_BASE_PATH%/}/$instance"
  ssh_cmd "$OFFSITE_SSH_TARGET" "mkdir -p '$remote_dir'"
  rsync -az "$archive_path" "$manifest_path" "$OFFSITE_SSH_TARGET:$remote_dir/"
  offsite_prune_instance "$instance"
}

offsite_prune_instance() {
  local instance="$1"
  local remote_dir="${OFFSITE_BASE_PATH%/}/$instance"
  local keep="$OFFSITE_KEEP_SNAPSHOTS"

  ssh_cmd "$OFFSITE_SSH_TARGET" "
    cd '$remote_dir' 2>/dev/null || exit 0
    ls -1t longhouse.*.sqlite.* 2>/dev/null | tail -n +$((keep + 1)) | while read -r f; do
      ts=\"\${f#longhouse.}\"
      ts=\"\${ts%.sqlite.*}\"
      rm -f \"\$f\" \"longhouse.\$ts.manifest.json\"
    done
  " || log "WARNING offsite prune failed for $instance"
}

snapshot_instance() {
  local instance="$1"
  local source_db="$LIVE_ROOT/$instance/longhouse.db"
  [[ -f "$source_db" ]] || {
    log "skip $instance (no longhouse.db at $source_db)"
    return 0
  }

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

  if ! remote_sync_snapshot "$instance" "$archive_final" "$manifest_final"; then
    log "WARNING offsite sync failed for $instance (local backup remains valid)"
  else
    if offsite_enabled; then
      log "offsite sync complete instance=$instance target=$OFFSITE_SSH_TARGET"
    fi
  fi

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
  [[ -d "$instance_dir" ]] || {
    log "verify skip $instance (no backup dir)"
    return 0
  }

  local latest
  latest="$(ls -1t "$instance_dir"/longhouse.*.sqlite.* 2>/dev/null | head -n1 || true)"
  [[ -n "$latest" ]] || {
    log "verify skip $instance (no backup archives)"
    return 0
  }

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

  if offsite_enabled; then
    local archive_name local_archive local_size remote_dir remote_archive remote_manifest remote_size
    archive_name="$(manifest_get_archive_name "$manifest")"
    local_archive="$instance_dir/$archive_name"
    if [[ ! -f "$local_archive" ]]; then
      log "ERROR monitor instance=$instance local archive missing $archive_name"
      return 1
    fi

    remote_dir="${OFFSITE_BASE_PATH%/}/$instance"
    remote_archive="$remote_dir/$archive_name"
    remote_manifest="$remote_dir/longhouse.$ts.manifest.json"

    if ! ssh_cmd "$OFFSITE_SSH_TARGET" "test -f '$remote_archive' && test -f '$remote_manifest'"; then
      log "ERROR monitor instance=$instance offsite artifacts missing ts=$ts"
      return 1
    fi

    local_size="$(stat_bytes "$local_archive")"
    remote_size="$(
      ssh_cmd "$OFFSITE_SSH_TARGET" \
        "if stat -c%s '$remote_archive' >/dev/null 2>&1; then stat -c%s '$remote_archive'; else stat -f%z '$remote_archive'; fi"
    )"

    if [[ "$local_size" != "$remote_size" ]]; then
      log "ERROR monitor instance=$instance offsite_size_mismatch local=$local_size remote=$remote_size archive=$archive_name"
      return 1
    fi
  fi

  log "monitor ok instance=$instance age_hours=$age_hours archive=$(basename "$latest_archive")"
  return 0
}

monitor_all() {
  local fail=0 count=0 instance

  local monitor_instances
  if ((${#TARGET_INSTANCES[@]} > 0)); then
    monitor_instances="$(discover_instances_from_targets || true)"
  else
    monitor_instances="$({ discover_live_instances; discover_backup_instances; } | awk 'NF > 0' | sort -u)"
  fi

  while IFS= read -r instance; do
    [[ -n "$instance" ]] || continue
    count=$((count + 1))
    if ! monitor_instance "$instance"; then
      fail=$((fail + 1))
    fi
  done <<<"$monitor_instances"

  if (( count == 0 )); then
    log "ERROR monitor found no instances to check"
    return 1
  fi

  if (( fail > 0 )); then
    log "ERROR monitor failures=$fail checked_instances=$count"
    return 1
  fi

  log "monitor success checked_instances=$count"
  return 0
}

cleanup_tmp_artifacts() {
  mkdir -p "$TMP_BACKUP_DIR"

  local file
  while IFS= read -r file; do
    [[ -n "$file" ]] || continue
    rm -f "$file"
    log "deleted old tmp artifact $file"
  done < <(find "$TMP_BACKUP_DIR" -type f -mtime +3 -print 2>/dev/null)
}

cleanup_docker() {
  if ! is_true "$ENABLE_DOCKER_PRUNE"; then
    log "docker prune disabled"
    return
  fi

  if ! command -v docker >/dev/null 2>&1; then
    log "docker not installed; skipping prune"
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

main() {
  parse_args "$@"

  mkdir -p "$BACKUP_ROOT" "$TMP_BACKUP_DIR"
  if [[ ! -d "$(dirname "$LOCK_FILE")" ]]; then
    LOCK_FILE="/tmp/zerg-ops.lock"
  fi

  if command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    flock -n 9 || {
      log "another zerg-ops run is active; exiting"
      exit 0
    }
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
      backup_all
      verify_latest_all
      prune_all_local
      cleanup_tmp_artifacts
      cleanup_docker
      report
      ;;
    backup)
      backup_all
      if is_true "$VERIFY_ON_BACKUP"; then
        verify_latest_all
      fi
      prune_all_local
      cleanup_tmp_artifacts
      ;;
    verify)
      verify_latest_all
      ;;
    monitor)
      monitor_all
      ;;
    cleanup)
      prune_all_local
      cleanup_tmp_artifacts
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

main "$@"
