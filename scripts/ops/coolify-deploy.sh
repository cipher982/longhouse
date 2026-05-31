#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
coolify-deploy.sh - trigger a Coolify application deploy and wait for completion

Usage:
  ./scripts/ops/coolify-deploy.sh <app-name-or-uuid> [--timeout 900] [--docker-image IMAGE --docker-tag TAG]

Environment:
  COOLIFY_API_HOST   SSH host that has access to the Coolify API token
                     Default: deploy-host
  COOLIFY_API_BASE   Coolify API base URL on the API host
                     Default: http://localhost:8000/api/v1
  COOLIFY_TIMEOUT    Default timeout in seconds if --timeout is omitted
                     Default: 900
  COOLIFY_STATUS_NOT_FOUND_GRACE
                     Seconds to tolerate 404 from deployment status after trigger
                     Default: 180
USAGE
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

APP_ID=""
TIMEOUT="${COOLIFY_TIMEOUT:-900}"
COOLIFY_API_HOST="${COOLIFY_API_HOST:-deploy-host}"
COOLIFY_API_BASE="${COOLIFY_API_BASE:-http://localhost:8000/api/v1}"
POLL_INTERVAL=5
STATUS_POLL_ERROR_BUDGET="${COOLIFY_STATUS_POLL_ERROR_BUDGET:-6}"
STATUS_NOT_FOUND_GRACE="${COOLIFY_STATUS_NOT_FOUND_GRACE:-180}"
DOCKER_IMAGE=""
DOCKER_TAG=""

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

load_token() {
  ssh "$COOLIFY_API_HOST" "sudo cat /var/lib/docker/data/coolify-api/token.env | cut -d= -f2"
}

api_get() {
  local path="$1"
  ssh "$COOLIFY_API_HOST" \
    "curl -fsS -H 'Authorization: Bearer ${COOLIFY_TOKEN}' '${COOLIFY_API_BASE}${path}'"
}

api_post_json() {
  local path="$1"
  local payload="$2"
  ssh "$COOLIFY_API_HOST" \
    "curl -fsS -X POST -H 'Authorization: Bearer ${COOLIFY_TOKEN}' -H 'Content-Type: application/json' -d '${payload}' '${COOLIFY_API_BASE}${path}'"
}

api_patch_json() {
  local path="$1"
  local payload="$2"
  ssh "$COOLIFY_API_HOST" \
    "curl -fsS -X PATCH -H 'Authorization: Bearer ${COOLIFY_TOKEN}' -H 'Content-Type: application/json' -d '${payload}' '${COOLIFY_API_BASE}${path}'"
}

resolve_app_uuid() {
  local candidate="$1"
  if [[ "$candidate" =~ ^[a-z0-9]{24}$ ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  local applications_json
  applications_json="$(api_get "/applications")"

  python3 -c '
import json
import sys

target = sys.argv[1]
for app in json.load(sys.stdin):
    if app.get("name") == target:
        print(app["uuid"])
        raise SystemExit(0)

raise SystemExit(f"Could not find Coolify application named {target!r}")
' "$candidate" <<<"$applications_json"
}

build_app_update_payload() {
  local image_name="$1"
  local image_tag="$2"
  python3 - "$image_name" "$image_tag" <<'PY'
import json
import sys

print(json.dumps({
    "docker_registry_image_name": sys.argv[1],
    "docker_registry_image_tag": sys.argv[2],
}))
PY
}

extract_deploy_uuid() {
  python3 -c '
import json
import sys

payload = json.load(sys.stdin)
deployments = payload.get("deployments") or []
if not deployments:
    raise SystemExit("Coolify deploy response did not include deployments")

deployment_uuid = deployments[0].get("deployment_uuid")
if not deployment_uuid:
    raise SystemExit("Coolify deploy response did not include deployment_uuid")

print(deployment_uuid)
'
}

deployment_status() {
  python3 -c '
import json
import sys

payload = json.load(sys.stdin)
print(payload.get("status", "unknown"))
'
}

print_recent_logs() {
  python3 -c '
import json
import sys

payload = json.load(sys.stdin)
logs = payload.get("logs")
if not logs:
    raise SystemExit(0)

if isinstance(logs, str):
    try:
        logs = json.loads(logs)
    except Exception:
        print(logs[-2000:])
        raise SystemExit(0)

if not isinstance(logs, list):
    raise SystemExit(0)

for entry in logs[-10:]:
    output = entry.get("output")
    if output:
        print(output.rstrip())
'
}

update_app_source_if_requested() {
  if [[ -z "$DOCKER_IMAGE" ]]; then
    return 0
  fi

  local payload
  payload="$(build_app_update_payload "$DOCKER_IMAGE" "$DOCKER_TAG")"
  echo "Configuring Coolify app source: ${DOCKER_IMAGE}:${DOCKER_TAG}"
  api_patch_json "/applications/${APP_UUID}" "$payload" >/dev/null
}

wait_for_deployment_completion() {
  local deploy_uuid="$1"
  local start_epoch elapsed status_response status
  local status_error_file status_error consecutive_status_errors

  status_error_file="$(mktemp)"
  consecutive_status_errors=0
  start_epoch="$(date +%s)"

  while true; do
    elapsed="$(( $(date +%s) - start_epoch ))"
    if (( elapsed >= TIMEOUT )); then
      rm -f "$status_error_file"
      echo "Timed out waiting for Coolify deployment ${deploy_uuid}" >&2
      return 2
    fi

    : >"$status_error_file"
    if ! status_response="$(api_get "/deployments/${deploy_uuid}" 2>"$status_error_file")"; then
      status_error="$(tr '\r\n' '  ' <"$status_error_file" | sed 's/[[:space:]]\+/ /g; s/^ //; s/ $//')"
      if [[ "$status_error" == *"404"* && "$elapsed" -lt "$STATUS_NOT_FOUND_GRACE" ]]; then
        echo ""
        echo "Coolify deployment status not found yet for ${deploy_uuid}; still within ${STATUS_NOT_FOUND_GRACE}s grace: ${status_error:-unknown error}" >&2
        sleep "$POLL_INTERVAL"
        continue
      fi
      consecutive_status_errors="$(( consecutive_status_errors + 1 ))"
      echo ""
      echo "Coolify status poll failed (${consecutive_status_errors}/${STATUS_POLL_ERROR_BUDGET}) while waiting for deployment ${deploy_uuid}: ${status_error:-unknown error}" >&2
      if (( consecutive_status_errors >= STATUS_POLL_ERROR_BUDGET )); then
        rm -f "$status_error_file"
        echo "Aborting after ${consecutive_status_errors} consecutive Coolify status poll failures." >&2
        return 1
      fi
      sleep "$POLL_INTERVAL"
      continue
    fi

    consecutive_status_errors=0
    status="$(deployment_status <<<"$status_response")"

    case "$status" in
      finished)
        rm -f "$status_error_file"
        echo ""
        echo "Coolify deploy finished in ${elapsed}s"
        return 0
        ;;
      failed|cancelled*)
        rm -f "$status_error_file"
        echo ""
        echo "Coolify deploy ${status} after ${elapsed}s" >&2
        print_recent_logs <<<"$status_response" >&2 || true
        return 1
        ;;
      queued|in_progress)
        printf "\r  status=%-12s elapsed=%ss" "$status" "$elapsed"
        sleep "$POLL_INTERVAL"
        ;;
      *)
        printf "\r  status=%-12s elapsed=%ss" "$status" "$elapsed"
        sleep "$POLL_INTERVAL"
        ;;
    esac
  done
}

main() {
  parse_args "$@"

  need_cmd ssh
  need_cmd python3

  COOLIFY_TOKEN="$(load_token)"
  APP_UUID="$(resolve_app_uuid "$APP_ID")"

  update_app_source_if_requested

  echo "Triggering Coolify deploy for ${APP_ID} (${APP_UUID})..."
  DEPLOY_RESPONSE="$(api_post_json "/deploy" "{\"uuid\":\"${APP_UUID}\"}")"
  DEPLOY_UUID="$(extract_deploy_uuid <<<"$DEPLOY_RESPONSE")"

  echo "Deployment UUID: ${DEPLOY_UUID}"
  echo "Waiting for Coolify deployment completion (timeout: ${TIMEOUT}s)..."
  wait_for_deployment_completion "$DEPLOY_UUID"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
