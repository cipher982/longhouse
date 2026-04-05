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

echo "coolify deploy helper tests passed"
