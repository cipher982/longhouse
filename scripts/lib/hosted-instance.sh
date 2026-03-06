#!/usr/bin/env bash

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

lh_hosted_require_env() {
  local name=""
  for name in "$@"; do
    if [[ -z "${!name:-}" ]]; then
      echo "Missing required environment variable: ${name}" >&2
      return 1
    fi
  done
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

  lh_hosted_require_env CONTROL_PLANE_URL CONTROL_PLANE_ADMIN_TOKEN || return 1

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

  lh_hosted_require_env CONTROL_PLANE_URL CONTROL_PLANE_ADMIN_TOKEN || return 1

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

  if [[ -z "$token" || -z "$cookie_jar" || -z "$instance_url" ]]; then
    echo "Usage: lh_hosted_accept_login_token <token> <cookie_jar> [instance_url]" >&2
    return 1
  fi

  http_code="$(curl -sS -o /dev/null -w "%{http_code}" \
    -c "$cookie_jar" \
    -H "Content-Type: application/json" \
    -d "{\"token\":\"${token}\"}" \
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

lh_hosted_reprovision() {
  local instance_id="${1:-${LH_INSTANCE_ID:-}}"
  local response_file=""
  local http_code=""

  if [[ -z "$instance_id" ]]; then
    echo "Missing instance id for reprovision request" >&2
    return 1
  fi

  lh_hosted_require_env CONTROL_PLANE_URL CONTROL_PLANE_ADMIN_TOKEN || return 1

  response_file="$(mktemp)"
  http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
    -X POST \
    -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
    "${CONTROL_PLANE_URL%/}/api/instances/${instance_id}/reprovision")"

  if [[ "$http_code" != "200" ]]; then
    echo "Failed to reprovision instance ${instance_id} (HTTP ${http_code})" >&2
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  fi

  rm -f "$response_file"
}
