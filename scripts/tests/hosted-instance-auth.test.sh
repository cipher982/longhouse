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

# Keep this test focused on explicit env-token fallback behavior instead of
# ambient operator access to the control plane via `ssh runtime-host`.
ssh() {
  return 255
}

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

cat >"$temp_json" <<'JSON'
{"id":7,"url":"https://demo.longhouse.ai","subdomain":"demo","status":"active","container_name":"longhouse-demo","data_path":"/var/app-data/longhouse/demo","password":"pw-123"}
JSON

parsed="$(_lh_hosted_parse_instance_payload "$temp_json")"
if [[ "$parsed" != $'7\thttps://demo.longhouse.ai\tdemo\tactive\tlonghouse-demo\t/var/app-data/longhouse/demo\tpw-123' ]]; then
  echo "Expected instance payload parser to include data_path"
  exit 1
fi

redirect_url="$(_lh_hosted_build_accept_token_redirect_url 'tok+/=' '/loop/card/demo?view=compact' 'https://demo.longhouse.ai')"
if [[ "$redirect_url" != 'https://demo.longhouse.ai/api/auth/accept-token?token=tok%2B%2F%3D&return_to=%2Floop%2Fcard%2Fdemo%3Fview%3Dcompact' ]]; then
  echo "Expected redirect URL builder to encode token and return_to safely"
  exit 1
fi

curl() {
  local headers_file=""
  local cookie_jar=""
  local request_url=""

  while [[ $# -gt 0 ]]; do
    case "$1" in
      -D)
        headers_file="$2"
        shift 2
        ;;
      -c)
        cookie_jar="$2"
        shift 2
        ;;
      -o|-w)
        shift 2
        ;;
      *)
        request_url="$1"
        shift
        ;;
    esac
  done

  printf '%s' "$request_url" >"$temp_json.request"
  printf 'HTTP/2 302\r\nlocation: /loop/card/123\r\nset-cookie: longhouse_session=test\r\n\r\n' >"$headers_file"
  : >"$cookie_jar"
  printf '302'
}

cookie_jar="$(mktemp)"
trap 'rm -f "$temp_json" "$cookie_jar" "$temp_json.request"' EXIT

redirect_location="$(lh_hosted_accept_login_token_redirect 'tok+/=' "$cookie_jar" '/loop?view=compact' 'https://demo.longhouse.ai')"
if [[ "$redirect_location" != '/loop/card/123' ]]; then
  echo "Expected redirect helper to return the Location header"
  exit 1
fi

if [[ "$(cat "$temp_json.request")" != 'https://demo.longhouse.ai/api/auth/accept-token?token=tok%2B%2F%3D&return_to=%2Floop%3Fview%3Dcompact' ]]; then
  echo "Expected redirect helper to call accept-token with encoded return_to"
  exit 1
fi

curl() {
  local data=""
  local output_file=""
  local request_url=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -d)
        data="$2"
        shift 2
        ;;
      -o)
        output_file="$2"
        shift 2
        ;;
      -w|-H|-X|--connect-timeout|--max-time)
        shift 2
        ;;
      *)
        request_url="$1"
        shift
        ;;
    esac
  done

  case "$request_url" in
    */api/instances/7/reprovision)
      printf '%s' "$request_url" >"$temp_json.request"
      printf '%s' "$data" >"$temp_json.body"
      printf '200'
      ;;
    */api/health)
      printf '%s' "$request_url" >"$temp_json.health-request"
      printf '{"build":{"commit":"deadbeef"}}' >"$output_file"
      printf '200'
      ;;
    *)
      echo "Unexpected curl URL in reprovision success wait test: $request_url" >&2
      return 1
      ;;
  esac
}

export INSTANCE_SUBDOMAIN="demo"
lh_hosted_reprovision "7" "ghcr.io/cipher982/longhouse-runtime:deadbeef"

if [[ "$(cat "$temp_json.request")" != 'https://control.longhouse.ai/api/instances/7/reprovision' ]]; then
  echo "Expected reprovision helper to target the instance reprovision endpoint"
  exit 1
fi

if [[ "$(cat "$temp_json.body")" != '{"image":"ghcr.io/cipher982/longhouse-runtime:deadbeef"}' ]]; then
  echo "Expected reprovision helper to send image override JSON"
  exit 1
fi

if [[ "$(cat "$temp_json.health-request")" != 'https://demo.longhouse.ai/api/health' ]]; then
  echo "Expected successful reprovision to wait until hosted runtime health reports the image"
  exit 1
fi

curl() {
  local data=""
  local output_file=""
  local request_url=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -d)
        data="$2"
        shift 2
        ;;
      -o)
        output_file="$2"
        shift 2
        ;;
      -w|-H|-X|--connect-timeout|--max-time)
        shift 2
        ;;
      *)
        request_url="$1"
        shift
        ;;
    esac
  done

  case "$request_url" in
    */api/instances/7/reprovision)
      printf '%s' "$request_url" >"$temp_json.request"
      printf '%s' "$data" >"$temp_json.body"
      printf 'cloudflare timeout' >"$output_file"
      printf '524'
      ;;
    */api/health)
      printf '%s' "$request_url" >"$temp_json.health-request"
      printf '{"build":{"commit":"deadbeefcafebabedeadbeefcafebabedeadbeef"}}' >"$output_file"
      printf '200'
      ;;
    *)
      echo "Unexpected curl URL in reprovision timeout fallback test: $request_url" >&2
      return 1
      ;;
  esac
}

lh_hosted_reprovision "7" "ghcr.io/cipher982/longhouse-runtime:deadbeefcafebabedeadbeefcafebabedeadbeef"

if [[ "$(cat "$temp_json.health-request")" != 'https://demo.longhouse.ai/api/health' ]]; then
  echo "Expected reprovision timeout fallback to poll hosted runtime health"
  exit 1
fi

echo "hosted-instance auth tests passed"
