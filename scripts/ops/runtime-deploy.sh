#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
runtime-deploy.sh - deploy a direct Longhouse runtime service on zerg

Usage:
  ./scripts/ops/runtime-deploy.sh longhouse-demo [--timeout 900] [--docker-image IMAGE --docker-tag TAG]

Environment:
  RUNTIME_HOST                 SSH host for zerg. Default: runtime-host
  LONGHOUSE_MANUAL_APPS_ROOT   Remote manual apps root. Default: /home/zerg/manual-apps
USAGE
}

APP_ID=""
TIMEOUT="${RUNTIME_DEPLOY_TIMEOUT:-900}"
DOCKER_IMAGE=""
DOCKER_TAG=""
RUNTIME_HOST="${RUNTIME_HOST:-runtime-host}"
REMOTE_ROOT="${LONGHOUSE_MANUAL_APPS_ROOT:-/home/zerg/manual-apps}"

parse_args() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi

  APP_ID="${1:-}"
  if [[ $# -gt 0 ]]; then
    shift
  fi

  while [[ $# -gt 0 ]]; do
    case "$1" in
      --timeout)
        TIMEOUT="${2:-}"
        shift 2
        ;;
      --docker-image)
        DOCKER_IMAGE="${2:-}"
        shift 2
        ;;
      --docker-tag)
        DOCKER_TAG="${2:-}"
        shift 2
        ;;
      -h|--help)
        usage
        exit 0
        ;;
      *)
        echo "Unknown argument: $1" >&2
        usage >&2
        exit 1
        ;;
    esac
  done

  if [[ -z "$APP_ID" ]]; then
    usage >&2
    exit 1
  fi

  if [[ -n "$DOCKER_IMAGE" || -n "$DOCKER_TAG" ]]; then
    if [[ -z "$DOCKER_IMAGE" || -z "$DOCKER_TAG" ]]; then
      echo "--docker-image and --docker-tag must be provided together" >&2
      exit 1
    fi
  fi
}

compose_var_for_app() {
  case "$1" in
    longhouse-demo)
      echo "LONGHOUSE_DEMO_IMAGE"
      ;;
    longhouse-control-plane)
      echo "LONGHOUSE_CONTROL_PLANE_IMAGE"
      ;;
    *)
      echo "Unsupported runtime app: $1" >&2
      return 1
      ;;
  esac
}

update_remote_image_pin() {
  local app="$1"
  local var_name="$2"
  local image_ref="$3"
  local remote_dir="${REMOTE_ROOT}/${app}"

  ssh "$RUNTIME_HOST" "mkdir -p '$remote_dir' && python3 - '$remote_dir/.env' '$var_name' '$image_ref' <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
key = sys.argv[2]
value = sys.argv[3]
lines = path.read_text().splitlines() if path.exists() else []
updated = False
out = []
for line in lines:
    if line.startswith(f'{key}='):
        out.append(f'{key}={value}')
        updated = True
    else:
        out.append(line)
if not updated:
    out.append(f'{key}={value}')
path.write_text('\n'.join(out) + '\n')
PY"
}

wait_for_container() {
  local app="$1"
  local deadline=$((SECONDS + TIMEOUT))
  local health=""
  local state=""

  while (( SECONDS < deadline )); do
    state="$(ssh "$RUNTIME_HOST" "docker inspect '$app' --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' 2>/dev/null" || true)"
    health="${state##* }"
    if [[ "$state" == running* && ( "$health" == "healthy" || "$health" == "none" ) ]]; then
      return 0
    fi
    sleep 5
  done

  echo "Timed out waiting for $app to become healthy" >&2
  ssh "$RUNTIME_HOST" "docker ps --filter 'name=$app' --format '{{.Names}} {{.Status}}'; docker logs --tail 80 '$app' 2>&1" >&2 || true
  return 1
}

main() {
  parse_args "$@"

  local var_name=""
  var_name="$(compose_var_for_app "$APP_ID")"

  if [[ -n "$DOCKER_IMAGE" ]]; then
    update_remote_image_pin "$APP_ID" "$var_name" "${DOCKER_IMAGE}:${DOCKER_TAG}"
  fi

  local remote_dir="${REMOTE_ROOT}/${APP_ID}"
  ssh "$RUNTIME_HOST" "cd '$remote_dir' && docker compose pull && docker compose up -d --remove-orphans"
  wait_for_container "$APP_ID"
}

main "$@"
