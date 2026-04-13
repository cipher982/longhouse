#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck disable=SC1091
source "$ROOT_DIR/ops/coolify-deploy.sh"

payload="$(build_app_update_payload 'ghcr.io/cipher982/longhouse-runtime' '0123456789abcdef0123456789abcdef01234567')"
expected='{"docker_registry_image_name":"ghcr.io/cipher982/longhouse-runtime","docker_registry_image_tag":"0123456789abcdef0123456789abcdef01234567"}'

if [[ "$(python3 -m json.tool <<<"$payload" | tr -d '\n[:space:]')" != "$(python3 -m json.tool <<<"$expected" | tr -d '\n[:space:]')" ]]; then
  echo "Expected docker image payload to preserve the image ref without mutating build_pack"
  exit 1
fi

DOCKER_IMAGE=""
DOCKER_TAG=""
APP_ID=""
TIMEOUT=900

if (parse_args longhouse-demo --docker-image ghcr.io/cipher982/longhouse-runtime) >/dev/null 2>&1; then
  echo "Expected parse_args to reject missing --docker-tag"
  exit 1
fi

DOCKER_IMAGE=""
DOCKER_TAG=""
APP_ID=""
TIMEOUT=900
parse_args longhouse-demo --docker-image ghcr.io/cipher982/longhouse-runtime --docker-tag abc123 --timeout 42

if [[ "$APP_ID" != "longhouse-demo" ]]; then
  echo "Expected APP_ID to be parsed"
  exit 1
fi

if [[ "$DOCKER_IMAGE" != "ghcr.io/cipher982/longhouse-runtime" || "$DOCKER_TAG" != "abc123" || "$TIMEOUT" != "42" ]]; then
  echo "Expected docker image args and timeout to parse correctly"
  exit 1
fi

TIMEOUT=30
POLL_INTERVAL=0
STATUS_POLL_ERROR_BUDGET=2
api_get_calls_file="$(mktemp)"
printf '0\n' >"$api_get_calls_file"

api_get() {
  local path="${1:-}"
  local attempt=""
  if [[ "$path" != "/deployments/test-deploy" ]]; then
    echo "Unexpected deployment status path: $path"
    return 1
  fi

  attempt="$(( $(<"$api_get_calls_file") + 1 ))"
  printf '%s\n' "$attempt" >"$api_get_calls_file"
  if [[ "$attempt" -eq 1 ]]; then
    echo "curl: (22) The requested URL returned error: 500" >&2
    return 22
  fi

  printf '{"status":"finished"}'
}

if ! wait_for_deployment_completion test-deploy >/dev/null 2>&1; then
  echo "Expected deployment wait helper to recover after a transient poll failure"
  exit 1
fi

if [[ "$(<"$api_get_calls_file")" != "2" ]]; then
  echo "Expected exactly one transient status failure before a successful poll"
  exit 1
fi

printf '0\n' >"$api_get_calls_file"
api_get() {
  local attempt=""
  attempt="$(( $(<"$api_get_calls_file") + 1 ))"
  printf '%s\n' "$attempt" >"$api_get_calls_file"
  echo "curl: (22) The requested URL returned error: 500" >&2
  return 22
}

if wait_for_deployment_completion test-deploy >/dev/null 2>&1; then
  echo "Expected deployment wait helper to fail after repeated status poll errors"
  exit 1
fi

if [[ "$(<"$api_get_calls_file")" != "$STATUS_POLL_ERROR_BUDGET" ]]; then
  echo "Expected deployment wait helper to stop after exhausting the poll error budget"
  exit 1
fi

rm -f "$api_get_calls_file"

echo "coolify deploy helper tests passed"
