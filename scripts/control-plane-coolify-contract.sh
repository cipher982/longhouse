#!/usr/bin/env bash
set -euo pipefail

APP_NAME="${APP_NAME:-longhouse-control-plane}"
COOLIFY_DB_HOST="${COOLIFY_DB_HOST:-clifford}"
COOLIFY_API_HOST="${COOLIFY_API_HOST:-$COOLIFY_DB_HOST}"
APP_HOST="${APP_HOST:-zerg}"
TARGET_DB_URL="${TARGET_DB_URL:-sqlite:////data/control-plane.db}"
TARGET_DB_DIR="${TARGET_DB_DIR:-/var/app-data/longhouse-control-plane}"
BACKUP_DB_GLOB="${BACKUP_DB_GLOB:-/var/app-data/longhouse-backups/control-plane/control-plane-*.db}"
TARGET_INSTANCE_DATA_ROOT="${TARGET_INSTANCE_DATA_ROOT:-/var/app-data/longhouse}"
DATA_MOUNT_PATH="/data"
INSTANCE_MOUNT_PATH="/var/app-data/longhouse"

usage() {
  cat <<'USAGE'
control-plane-coolify-contract.sh - assert/fix the deployed Longhouse control-plane Coolify contract

Usage:
  ./scripts/control-plane-coolify-contract.sh check
  ./scripts/control-plane-coolify-contract.sh apply

Checks/fixes:
  - CONTROL_PLANE_DATABASE_URL regular env = sqlite:////data/control-plane.db
  - CONTROL_PLANE_INSTANCE_DATA_ROOT regular env = /var/app-data/longhouse
  - Preview env rows for those keys are absent or match the prod values
  - /data storage points at /var/app-data/longhouse-control-plane
  - /var/app-data/longhouse storage points at /var/app-data/longhouse
USAGE
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: missing required command: $1" >&2
    exit 1
  }
}

say() {
  printf '%s\n' "$*"
}

warn() {
  printf 'WARN: %s\n' "$*" >&2
}

load_coolify_token() {
  ssh "$COOLIFY_API_HOST" "sudo cat /var/lib/docker/data/coolify-api/token.env | cut -d= -f2"
}

resolve_app_uuid() {
  coolify app list --format json | jq -r --arg name "$APP_NAME" '.[] | select(.name == $name) | .uuid' | head -1
}

load_envs_json() {
  coolify app env list "$APP_UUID" --all --format json -s
}

regular_env_value() {
  local key="$1"
  jq -r --arg key "$key" '.[] | select(.key == $key and (.is_preview | not)) | (.real_value // .value)' <<<"$ENVS_JSON" \
    | head -1 \
    | sed "s/^['\"]//; s/['\"]$//"
}

matching_env_uuids() {
  local key="$1"
  local preview="$2"
  jq -r --arg key "$key" --argjson preview "$preview" '.[] | select(.key == $key and .is_preview == $preview) | .uuid' <<<"$ENVS_JSON"
}

sql_scalar() {
  local sql="$1"
  ssh "$COOLIFY_DB_HOST" "docker exec coolify-db psql -U coolify -d coolify -t -A -c \"$sql\"" | tr -d '\r' | sed '/^$/d' | head -1
}

storage_host_path() {
  local mount_path="$1"
  sql_scalar "SELECT host_path FROM local_persistent_volumes WHERE resource_type='App\\Models\\Application' AND resource_id=(SELECT id FROM applications WHERE uuid='${APP_UUID}') AND mount_path='${mount_path}' LIMIT 1;"
}

set_storage_host_path() {
  local mount_path="$1"
  local host_path="$2"
  sql_scalar "UPDATE local_persistent_volumes SET host_path='${host_path}', updated_at=NOW() WHERE resource_type='App\\Models\\Application' AND resource_id=(SELECT id FROM applications WHERE uuid='${APP_UUID}') AND mount_path='${mount_path}'; SELECT host_path FROM local_persistent_volumes WHERE resource_type='App\\Models\\Application' AND resource_id=(SELECT id FROM applications WHERE uuid='${APP_UUID}') AND mount_path='${mount_path}' LIMIT 1;" >/dev/null
}

print_check() {
  local label="$1"
  local current="$2"
  local expected="$3"
  if [[ "$current" == "$expected" ]]; then
    say "OK   $label = $current"
    return 0
  fi
  warn "$label mismatch: current=${current:-<missing>} expected=$expected"
  return 1
}

check_preview_env() {
  local key="$1"
  local expected="$2"
  local values
  values="$(jq -r --arg key "$key" '.[] | select(.key == $key and .is_preview) | (.real_value // .value)' <<<"$ENVS_JSON" | sed "s/^['\"]//; s/['\"]$//")"
  if [[ -z "$values" ]]; then
    say "OK   preview env $key absent"
    return 0
  fi
  local mismatches
  mismatches="$(printf '%s
' "$values" | awk -v expected="$expected" 'length($0) && $0 != expected { print }')"
  if [[ -n "$mismatches" ]]; then
    warn "preview env $key drifted from contract: ${mismatches//$'\n'/, }"
    return 1
  fi
  say "OK   preview env $key matches contract"
  return 0
}

check_contract() {
  ENVS_JSON="$(load_envs_json)"
  local issues=0

  local db_url
  db_url="$(regular_env_value CONTROL_PLANE_DATABASE_URL)"
  print_check "env CONTROL_PLANE_DATABASE_URL" "$db_url" "$TARGET_DB_URL" || issues=$((issues + 1))

  local instance_root
  instance_root="$(regular_env_value CONTROL_PLANE_INSTANCE_DATA_ROOT)"
  print_check "env CONTROL_PLANE_INSTANCE_DATA_ROOT" "$instance_root" "$TARGET_INSTANCE_DATA_ROOT" || issues=$((issues + 1))

  check_preview_env CONTROL_PLANE_DATABASE_URL "$TARGET_DB_URL" || issues=$((issues + 1))
  check_preview_env CONTROL_PLANE_INSTANCE_DATA_ROOT "$TARGET_INSTANCE_DATA_ROOT" || issues=$((issues + 1))

  local data_mount_host
  data_mount_host="$(storage_host_path "$DATA_MOUNT_PATH")"
  print_check "storage $DATA_MOUNT_PATH" "$data_mount_host" "$TARGET_DB_DIR" || issues=$((issues + 1))

  local instance_mount_host
  instance_mount_host="$(storage_host_path "$INSTANCE_MOUNT_PATH")"
  print_check "storage $INSTANCE_MOUNT_PATH" "$instance_mount_host" "$TARGET_INSTANCE_DATA_ROOT" || issues=$((issues + 1))

  return "$issues"
}

remove_envs() {
  local key="$1"
  local preview="$2"
  while IFS= read -r env_uuid; do
    [[ -n "$env_uuid" ]] || continue
    coolify app env delete "$APP_UUID" "$env_uuid" --force >/dev/null
  done < <(matching_env_uuids "$key" "$preview")
}

create_regular_env() {
  local key="$1"
  local value="$2"
  local is_literal="${3:-false}"
  local payload
  payload="$(jq -nc --arg key "$key" --arg value "$value" --argjson is_literal "$is_literal" '{key: $key, value: $value, is_buildtime: true, is_runtime: true, is_literal: $is_literal}')"
  ssh "$COOLIFY_API_HOST" "curl -fsS -X POST -H 'Authorization: Bearer $COOLIFY_TOKEN' -H 'Content-Type: application/json' -d '$payload' 'http://localhost:8000/api/v1/applications/$APP_UUID/envs'" >/dev/null
}

remote_cp_instances() {
  local db_path="$1"
  ssh "$APP_HOST" "if [ -f '$db_path' ]; then sudo sqlite3 '$db_path' 'select count(*) from cp_instances;' 2>/dev/null || true; fi" | tr -d '\r' | tr -d '[:space:]'
}

db_has_instances() {
  local db_path="$1"
  local count
  count="$(remote_cp_instances "$db_path")"
  [[ "$count" =~ ^[0-9]+$ ]] && (( count > 0 ))
}

latest_backup_db() {
  ssh "$APP_HOST" "ls -1t $BACKUP_DB_GLOB 2>/dev/null | head -1" | tr -d '\r'
}

stage_control_plane_db() {
  local target_db="$TARGET_DB_DIR/control-plane.db"
  local backup_db=""

  ssh "$APP_HOST" "sudo install -d -m 755 '$TARGET_DB_DIR'"

  if db_has_instances "$target_db"; then
    say "Target DB already populated; leaving it in place."
    return 0
  fi

  backup_db="$(latest_backup_db)"
  if [[ -n "$backup_db" ]] && db_has_instances "$backup_db"; then
    say "Restoring populated backup $backup_db into $TARGET_DB_DIR..."
    ssh "$APP_HOST" "sudo install -m 600 '$backup_db' '$target_db'"
    return 0
  fi

  warn "Target DB is empty and no populated source was found."
  warn "Checked backup glob: $BACKUP_DB_GLOB"
  echo "ERROR: refusing to stage an empty control-plane DB" >&2
  exit 1
}

apply_contract() {
  say "Staging control-plane SQLite DB onto app-data..."
  stage_control_plane_db

  ENVS_JSON="$(load_envs_json)"
  say "Removing preview env overrides for contract keys..."
  remove_envs CONTROL_PLANE_DATABASE_URL true
  remove_envs CONTROL_PLANE_INSTANCE_DATA_ROOT true

  ENVS_JSON="$(load_envs_json)"
  say "Recreating regular env rows for contract keys..."
  remove_envs CONTROL_PLANE_DATABASE_URL false
  remove_envs CONTROL_PLANE_INSTANCE_DATA_ROOT false
  create_regular_env CONTROL_PLANE_DATABASE_URL "$TARGET_DB_URL" true
  create_regular_env CONTROL_PLANE_INSTANCE_DATA_ROOT "$TARGET_INSTANCE_DATA_ROOT" false

  say "Updating Coolify storage rows..."
  set_storage_host_path "$DATA_MOUNT_PATH" "$TARGET_DB_DIR"
  set_storage_host_path "$INSTANCE_MOUNT_PATH" "$TARGET_INSTANCE_DATA_ROOT"

  say "Deploying $APP_NAME..."
  ~/git/me/mytech/scripts/coolify-deploy.sh "$APP_NAME"

  say "Verifying control-plane health and DB location..."
  curl -fsS https://control.longhouse.ai/health >/dev/null
  ssh "$APP_HOST" "sudo test -f '$TARGET_DB_DIR/control-plane.db'"

  ENVS_JSON="$(load_envs_json)"
  check_contract
}

main() {
  local cmd="${1:-check}"
  case "$cmd" in
    -h|--help)
      usage
      exit 0
      ;;
    check|apply)
      ;;
    *)
      echo "ERROR: expected 'check' or 'apply', got '$cmd'" >&2
      usage >&2
      exit 1
      ;;
  esac

  need_cmd coolify
  need_cmd jq
  need_cmd ssh
  need_cmd curl
  coolify context verify >/dev/null

  COOLIFY_TOKEN="$(load_coolify_token)"
  APP_UUID="$(resolve_app_uuid)"
  [[ -n "$APP_UUID" ]] || {
    echo "ERROR: could not resolve Coolify app UUID for $APP_NAME" >&2
    exit 1
  }

  case "$cmd" in
    check)
      check_contract
      ;;
    apply)
      apply_contract
      ;;
  esac
}

main "$@"
