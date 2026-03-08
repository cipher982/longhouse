#!/usr/bin/env bash

LH_HOSTED_HELPER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LH_HOSTED_INFISICAL_HELPER="${LH_HOSTED_INFISICAL_HELPER:-$LH_HOSTED_HELPER_DIR/infisical.sh}"
if [[ -f "$LH_HOSTED_INFISICAL_HELPER" ]]; then
  # shellcheck disable=SC1090
  if ! . "$LH_HOSTED_INFISICAL_HELPER"; then
    echo "Failed to source Infisical helper: $LH_HOSTED_INFISICAL_HELPER" >&2
    return 1 2>/dev/null || exit 1
  fi
fi

_lh_hosted_python_bin() {
  if [[ -n "${LH_HOSTED_PYTHON_BIN:-}" ]]; then
    printf '%s\n' "$LH_HOSTED_PYTHON_BIN"
    return 0
  fi

  if command -v python3 >/dev/null 2>&1; then
    LH_HOSTED_PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    LH_HOSTED_PYTHON_BIN="python"
  else
    echo "Missing python3/python for hosted-instance helper" >&2
    return 1
  fi

  export LH_HOSTED_PYTHON_BIN
  printf '%s\n' "$LH_HOSTED_PYTHON_BIN"
}

_lh_hosted_json_object() {
  local python_bin
  python_bin="$(_lh_hosted_python_bin)" || return 1

  "$python_bin" - "$@" <<'PY'
import json
import sys

args = sys.argv[1:]
if len(args) % 2 != 0:
    raise SystemExit("Expected even key/value pairs")

payload = {}
for index in range(0, len(args), 2):
    payload[args[index]] = args[index + 1]

print(json.dumps(payload, separators=(",", ":")), end="")
PY
}

lh_hosted_require_env() {
  local name=""
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      echo "Missing required environment variable: ${name}" >&2
      return 1
    fi
  done
}

_lh_hosted_parse_instance_payload() {
  local response_file="$1"
  local python_bin
  python_bin="$(_lh_hosted_python_bin)" || return 1

  "$python_bin" - "$response_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

instance_id = payload.get("id")
url = payload.get("url")
if not instance_id or not url:
    sys.exit(3)


def clean(value):
    return str("" if value is None else value).replace("\t", " ").replace("\n", " ")

print("\t".join(
    [
        clean(instance_id),
        clean(url),
        clean(payload.get("subdomain")),
        clean(payload.get("status")),
        clean(payload.get("container_name")),
        clean(payload.get("password")),
    ]
))
PY
}

_lh_hosted_export_instance_payload() {
  local parsed="$1"
  local fallback_subdomain="${2:-}"

  IFS=$'\t' read -r LH_INSTANCE_ID LH_INSTANCE_URL LH_INSTANCE_SUBDOMAIN LH_INSTANCE_STATUS LH_INSTANCE_CONTAINER_NAME LH_INSTANCE_PASSWORD <<< "$parsed"
  if [[ -z "$LH_INSTANCE_SUBDOMAIN" && -n "$fallback_subdomain" ]]; then
    LH_INSTANCE_SUBDOMAIN="$fallback_subdomain"
  fi
  export LH_INSTANCE_ID LH_INSTANCE_URL LH_INSTANCE_SUBDOMAIN LH_INSTANCE_STATUS LH_INSTANCE_CONTAINER_NAME LH_INSTANCE_PASSWORD
}

_lh_hosted_parse_instance_row() {
  local response_file="$1"
  local subdomain="$2"
  local python_bin
  python_bin="$(_lh_hosted_python_bin)" || return 1

  "$python_bin" - "$response_file" "$subdomain" <<'PY'
import json
import sys

response_file, subdomain = sys.argv[1], sys.argv[2]
with open(response_file, encoding="utf-8") as handle:
    payload = json.load(handle)

for instance in payload.get("instances", []):
    if instance.get("subdomain") != subdomain:
        continue
    instance_id = instance.get("id")
    url = instance.get("url")
    if not instance_id or not url:
        sys.exit(3)
    print(f"{instance_id}\t{url}\t{subdomain}")
    sys.exit(0)

sys.exit(2)
PY
}

lh_hosted_resolve_instance() {
  local subdomain="$1"
  local response_file=""
  local http_code=""
  local parsed=""
  local parse_status=0

  lh_hosted_prepare_control_plane_auth || return 1

  response_file="$(mktemp)"
  http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
    -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
    "${CONTROL_PLANE_URL%/}/api/instances")"

  if [[ "$http_code" != "200" ]]; then
    echo "Failed to list control-plane instances (HTTP ${http_code})" >&2
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  fi

  if parsed="$(_lh_hosted_parse_instance_row "$response_file" "$subdomain")"; then
    :
  else
    parse_status=$?
    case "$parse_status" in
      2)
        echo "Instance not found for subdomain: ${subdomain}" >&2
        ;;
      3)
        echo "Control-plane instance payload missing id/url for subdomain: ${subdomain}" >&2
        ;;
      *)
        rm -f "$response_file"
        return 1
        ;;
    esac
    rm -f "$response_file"
    return 1
  fi

  rm -f "$response_file"
  IFS=$'\t' read -r LH_INSTANCE_ID LH_INSTANCE_URL LH_INSTANCE_SUBDOMAIN <<< "$parsed"
  export LH_INSTANCE_ID LH_INSTANCE_URL LH_INSTANCE_SUBDOMAIN
}

lh_hosted_default_control_plane_url() {
  CONTROL_PLANE_URL="${CONTROL_PLANE_URL:-${CP_URL:-https://control.longhouse.ai}}"
  CP_URL="$CONTROL_PLANE_URL"
  export CONTROL_PLANE_URL CP_URL
}

lh_hosted_prepare_control_plane_auth() {
  lh_hosted_default_control_plane_url
  CONTROL_PLANE_ADMIN_TOKEN="${CONTROL_PLANE_ADMIN_TOKEN:-${ADMIN_TOKEN:-}}"

  if [[ -z "${CONTROL_PLANE_ADMIN_TOKEN:-}" ]] && declare -F lh_infisical_export_secret_if_missing >/dev/null 2>&1; then
    if ! lh_infisical_export_secret_if_missing CONTROL_PLANE_ADMIN_TOKEN CONTROL_PLANE_ADMIN_TOKEN; then
      echo "Set CONTROL_PLANE_ADMIN_TOKEN/ADMIN_TOKEN or populate CONTROL_PLANE_ADMIN_TOKEN in Infisical ops-infra/prod and ensure infisical login works." >&2
      return 1
    fi
  fi

  export CONTROL_PLANE_ADMIN_TOKEN
  lh_hosted_require_env CONTROL_PLANE_URL CONTROL_PLANE_ADMIN_TOKEN
}

lh_hosted_create_instance() {
  local email="$1"
  local subdomain="$2"
  local response_file=""
  local http_code=""
  local parsed=""
  local parse_status=0
  local payload=""

  if [[ -z "$email" || -z "$subdomain" ]]; then
    echo "Usage: lh_hosted_create_instance <email> <subdomain>" >&2
    return 1
  fi

  lh_hosted_prepare_control_plane_auth || return 1
  payload="$(_lh_hosted_json_object email "$email" subdomain "$subdomain")" || return 1

  response_file="$(mktemp)"
  http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
    --connect-timeout 10 --max-time 60 \
    -X POST "${CONTROL_PLANE_URL%/}/api/instances" \
    -H "Content-Type: application/json" \
    -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
    -d "$payload")"

  if [[ "$http_code" != "200" && "$http_code" != "201" ]]; then
    echo "Failed to create instance ${subdomain} (HTTP ${http_code})" >&2
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  fi

  if parsed="$(_lh_hosted_parse_instance_payload "$response_file")"; then
    :
  else
    parse_status=$?
    if [[ "$parse_status" -eq 3 ]]; then
      echo "Create-instance response missing id/url for subdomain: ${subdomain}" >&2
    fi
    rm -f "$response_file"
    return 1
  fi

  rm -f "$response_file"
  _lh_hosted_export_instance_payload "$parsed" "$subdomain"
}

lh_hosted_get_instance() {
  local instance_id="${1:-${LH_INSTANCE_ID:-}}"
  local response_file=""
  local http_code=""
  local parsed=""
  local parse_status=0

  if [[ -z "$instance_id" ]]; then
    echo "Missing instance id for get-instance request" >&2
    return 1
  fi

  lh_hosted_prepare_control_plane_auth || return 1

  response_file="$(mktemp)"
  http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
    -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
    "${CONTROL_PLANE_URL%/}/api/instances/${instance_id}")"

  if [[ "$http_code" != "200" ]]; then
    echo "Failed to get instance ${instance_id} (HTTP ${http_code})" >&2
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  fi

  if parsed="$(_lh_hosted_parse_instance_payload "$response_file")"; then
    :
  else
    parse_status=$?
    if [[ "$parse_status" -eq 3 ]]; then
      echo "Get-instance response missing id/url for instance: ${instance_id}" >&2
    fi
    rm -f "$response_file"
    return 1
  fi

  rm -f "$response_file"
  _lh_hosted_export_instance_payload "$parsed"
}

lh_hosted_prepare_target() {
  local subdomain="${1:-}"
  local frontend_url="${2:-}"
  local api_url="${3:-}"
  local default_subdomain="${4:-}"

  lh_hosted_default_control_plane_url

  if [[ -z "$subdomain" && -z "$frontend_url" && -n "$default_subdomain" ]]; then
    subdomain="$default_subdomain"
  fi

  if [[ -n "$subdomain" ]]; then
    lh_hosted_resolve_instance "$subdomain" || return 1
    frontend_url="${frontend_url:-$LH_INSTANCE_URL}"
    api_url="${api_url:-${frontend_url}}"
    subdomain="$LH_INSTANCE_SUBDOMAIN"
  else
    api_url="${api_url:-$frontend_url}"
  fi

  if [[ -z "$frontend_url" || -z "$api_url" ]]; then
    echo "Set INSTANCE_SUBDOMAIN, CONTROL_PLANE_* (or Infisical ops-infra access), or FRONTEND_URL/API_URL before preparing hosted target." >&2
    return 1
  fi

  LH_TARGET_SUBDOMAIN="$subdomain"
  LH_TARGET_FRONTEND_URL="$frontend_url"
  LH_TARGET_API_URL="$api_url"
  export LH_TARGET_SUBDOMAIN LH_TARGET_FRONTEND_URL LH_TARGET_API_URL
}

lh_hosted_resolved_login_token() {
  local subdomain="${1:-${LH_TARGET_SUBDOMAIN:-${LH_INSTANCE_SUBDOMAIN:-}}}"

  if [[ -n "${SMOKE_LOGIN_TOKEN:-}" ]]; then
    printf '%s\n' "$SMOKE_LOGIN_TOKEN"
    return 0
  fi

  if [[ -z "$subdomain" ]]; then
    echo "Set SMOKE_LOGIN_TOKEN or INSTANCE_SUBDOMAIN + CONTROL_PLANE_* before requesting a hosted login token." >&2
    return 1
  fi

  if [[ -z "${LH_INSTANCE_ID:-}" || "${LH_INSTANCE_SUBDOMAIN:-}" != "$subdomain" ]]; then
    lh_hosted_resolve_instance "$subdomain" || return 1
  fi

  lh_hosted_issue_login_token "$LH_INSTANCE_ID"
}

_lh_hosted_parse_token() {
  local response_file="$1"
  local python_bin
  python_bin="$(_lh_hosted_python_bin)" || return 1

  "$python_bin" - "$response_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

token = payload.get("token")
if not token:
    sys.exit(1)
print(token)
PY
}

lh_hosted_issue_login_token() {
  local instance_id="${1:-${LH_INSTANCE_ID:-}}"
  local response_file=""
  local http_code=""
  local token=""

  if [[ -z "$instance_id" ]]; then
    echo "Missing instance id for login-token request" >&2
    return 1
  fi

  lh_hosted_prepare_control_plane_auth || return 1

  response_file="$(mktemp)"
  http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
    -X POST \
    -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
    "${CONTROL_PLANE_URL%/}/api/instances/${instance_id}/login-token")"

  if [[ "$http_code" != "200" ]]; then
    echo "Failed to issue login token for instance ${instance_id} (HTTP ${http_code})" >&2
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  fi

  token="$(_lh_hosted_parse_token "$response_file")" || {
    echo "Login-token response missing token for instance ${instance_id}" >&2
    rm -f "$response_file"
    return 1
  }
  rm -f "$response_file"
  printf '%s\n' "$token"
}

lh_hosted_accept_login_token() {
  local token="$1"
  local cookie_jar="$2"
  local instance_url="${3:-${LH_INSTANCE_URL:-}}"
  local http_code=""
  local payload=""

  if [[ -z "$token" || -z "$cookie_jar" || -z "$instance_url" ]]; then
    echo "Usage: lh_hosted_accept_login_token <token> <cookie_jar> [instance_url]" >&2
    return 1
  fi

  payload="$(_lh_hosted_json_object token "$token")" || return 1

  http_code="$(curl -sS -o /dev/null -w "%{http_code}" \
    -c "$cookie_jar" \
    -H "Content-Type: application/json" \
    -d "$payload" \
    "${instance_url%/}/api/auth/accept-token")"

  if [[ "$http_code" != "200" && "$http_code" != "302" ]]; then
    echo "Instance rejected login token at ${instance_url} (HTTP ${http_code})" >&2
    return 1
  fi
}

lh_hosted_authenticate_cookie_jar() {
  local subdomain="$1"
  local cookie_jar="$2"
  local token=""

  lh_hosted_resolve_instance "$subdomain" || return 1
  token="$(lh_hosted_issue_login_token "$LH_INSTANCE_ID")" || return 1
  lh_hosted_accept_login_token "$token" "$cookie_jar" "$LH_INSTANCE_URL"
}

_lh_hosted_post_instance_action() {
  local instance_id="$1"
  local action="$2"
  local response_file=""
  local http_code=""

  if [[ -z "$instance_id" ]]; then
    echo "Missing instance id for ${action} request" >&2
    return 1
  fi

  lh_hosted_prepare_control_plane_auth || return 1

  response_file="$(mktemp)"
  http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
    -X POST \
    -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
    "${CONTROL_PLANE_URL%/}/api/instances/${instance_id}/${action}")"

  if [[ "$http_code" != "200" ]]; then
    echo "Failed to ${action} instance ${instance_id} (HTTP ${http_code})" >&2
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  fi

  rm -f "$response_file"
}

lh_hosted_reprovision() {
  local instance_id="${1:-${LH_INSTANCE_ID:-}}"
  _lh_hosted_post_instance_action "$instance_id" "reprovision"
}

lh_hosted_deprovision() {
  local instance_id="${1:-${LH_INSTANCE_ID:-}}"
  _lh_hosted_post_instance_action "$instance_id" "deprovision"
}
