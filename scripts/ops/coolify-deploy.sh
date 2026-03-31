#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
coolify-deploy.sh - trigger a Coolify application deploy and wait for completion

Usage:
  ./scripts/ops/coolify-deploy.sh <app-name-or-uuid> [--timeout 900]

Environment:
  COOLIFY_API_HOST   SSH host that has access to the Coolify API token
                     Default: clifford
  COOLIFY_API_BASE   Coolify API base URL on the API host
                     Default: http://localhost:8000/api/v1
  COOLIFY_TIMEOUT    Default timeout in seconds if --timeout is omitted
                     Default: 900
USAGE
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

APP_ID="${1:-}"
if [[ $# -gt 0 ]]; then
  shift
fi

TIMEOUT="${COOLIFY_TIMEOUT:-900}"
COOLIFY_API_HOST="${COOLIFY_API_HOST:-clifford}"
COOLIFY_API_BASE="${COOLIFY_API_BASE:-http://localhost:8000/api/v1}"
POLL_INTERVAL=5

while [[ $# -gt 0 ]]; do
  case "$1" in
    --timeout)
      TIMEOUT="${2:-}"
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

need_cmd ssh
need_cmd python3

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

resolve_app_uuid() {
  local candidate="$1"
  if [[ "$candidate" =~ ^[a-z0-9]{24}$ ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  local applications_json
  applications_json="$(api_get "/applications")"

  python3 - "$candidate" <<'PY' <<<"$applications_json"
import json
import sys

target = sys.argv[1]
for app in json.load(sys.stdin):
    if app.get("name") == target:
        print(app["uuid"])
        raise SystemExit(0)

raise SystemExit(f"Could not find Coolify application named {target!r}")
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

COOLIFY_TOKEN="$(load_token)"
APP_UUID="$(resolve_app_uuid "$APP_ID")"

echo "Triggering Coolify deploy for ${APP_ID} (${APP_UUID})..."
DEPLOY_RESPONSE="$(api_post_json "/deploy" "{\"uuid\":\"${APP_UUID}\"}")"
DEPLOY_UUID="$(extract_deploy_uuid <<<"$DEPLOY_RESPONSE")"

echo "Deployment UUID: ${DEPLOY_UUID}"
echo "Waiting for Coolify deployment completion (timeout: ${TIMEOUT}s)..."

start_epoch="$(date +%s)"
while true; do
  elapsed="$(( $(date +%s) - start_epoch ))"
  if (( elapsed >= TIMEOUT )); then
    echo "Timed out waiting for Coolify deployment ${DEPLOY_UUID}" >&2
    exit 2
  fi

  STATUS_RESPONSE="$(api_get "/deployments/${DEPLOY_UUID}")"
  STATUS="$(deployment_status <<<"$STATUS_RESPONSE")"

  case "$STATUS" in
    finished)
      echo ""
      echo "Coolify deploy finished in ${elapsed}s"
      exit 0
      ;;
    failed|cancelled*)
      echo ""
      echo "Coolify deploy ${STATUS} after ${elapsed}s" >&2
      print_recent_logs <<<"$STATUS_RESPONSE" >&2 || true
      exit 1
      ;;
    queued|in_progress)
      printf "\r  status=%-12s elapsed=%ss" "$STATUS" "$elapsed"
      sleep "$POLL_INTERVAL"
      ;;
    *)
      printf "\r  status=%-12s elapsed=%ss" "$STATUS" "$elapsed"
      sleep "$POLL_INTERVAL"
      ;;
  esac
done
