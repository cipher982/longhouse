#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

unset CONTROL_PLANE_ADMIN_TOKEN
unset ADMIN_TOKEN
unset CONTROL_PLANE_URL
unset CP_URL

# shellcheck disable=SC1091
source "$ROOT_DIR/lib/hosted-instance.sh"

if lh_hosted_prepare_control_plane_auth >/dev/null 2>&1; then
  echo "Expected hosted auth prep to fail without explicit admin token"
  exit 1
fi

export ADMIN_TOKEN="admin-token-from-env"
lh_hosted_prepare_control_plane_auth >/dev/null

if [[ "$CONTROL_PLANE_ADMIN_TOKEN" != "admin-token-from-env" ]]; then
  echo "Expected ADMIN_TOKEN fallback to populate CONTROL_PLANE_ADMIN_TOKEN"
  exit 1
fi

if [[ "$CONTROL_PLANE_URL" != "https://control.longhouse.ai" ]]; then
  echo "Expected CONTROL_PLANE_URL default to be applied"
  exit 1
fi

json_payload="$(_lh_hosted_json_object email 'quote"@example.com' subdomain 'demo\slash')"
if [[ "$json_payload" != '{"email":"quote\"@example.com","subdomain":"demo\\slash"}' ]]; then
  echo "Expected hosted JSON helper to escape values safely"
  exit 1
fi

temp_json="$(mktemp)"
trap 'rm -f "$temp_json"' EXIT

cat >"$temp_json" <<'JSON'
{"access_token":"access-123"}
JSON

if [[ "$(_lh_hosted_parse_access_token "$temp_json")" != "access-123" ]]; then
  echo "Expected access-token parser to read access_token payload"
  exit 1
fi

cat >"$temp_json" <<'JSON'
{"id":"device-token-id","token":"zdt_smoke"}
JSON

if [[ "$(_lh_hosted_parse_device_token_payload "$temp_json")" != $'device-token-id\tzdt_smoke' ]]; then
  echo "Expected device-token parser to return token id and token"
  exit 1
fi

echo "hosted-instance auth tests passed"
