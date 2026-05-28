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

_lh_hosted_urlencode() {
  local python_bin
  python_bin="$(_lh_hosted_python_bin)" || return 1

  "$python_bin" - "$1" <<'PY'
import sys
import urllib.parse

print(urllib.parse.quote(sys.argv[1], safe=""), end="")
PY
}

lh_hosted_require_env() {
  local name=""
  for name in "$@"; do
    if [[ -z "$(printenv "$name")" ]]; then
      echo "Missing required environment variable: ${name}" >&2
      return 1
    fi
  done
}

_lh_hosted_is_retryable_http_code() {
  case "${1:-}" in
    000|408|409|425|429|500|502|503|504|520|521|522|523|524|525|526)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

_lh_hosted_retry_sleep() {
  local attempt="${1:-1}"
  local delay=1
  if [[ "$attempt" -gt 1 ]]; then
    delay="$attempt"
  fi
  if [[ "$delay" -gt 3 ]]; then
    delay=3
  fi
  sleep "$delay"
}

_lh_hosted_build_accept_token_redirect_url() {
  local token="$1"
  local return_to="${2:-}"
  local instance_url="${3:-${LH_INSTANCE_URL:-}}"
  local encoded_token=""
  local encoded_return_to=""
  local safe_return_to=""
  local python_bin=""

  if [[ -z "$token" || -z "$instance_url" ]]; then
    echo "Usage: _lh_hosted_build_accept_token_redirect_url <token> [return_to] [instance_url]" >&2
    return 1
  fi

  python_bin="$(_lh_hosted_python_bin)" || return 1
  encoded_token="$(_lh_hosted_urlencode "$token")" || return 1
  safe_return_to="$("$python_bin" - "$return_to" <<'PY'
import sys

value = sys.argv[1]
if not value or not value.startswith("/") or value.startswith("//"):
    print("", end="")
else:
    print(value, end="")
PY
)" || return 1

  local url="${instance_url%/}/api/auth/accept-token?token=${encoded_token}"
  if [[ -n "$safe_return_to" ]]; then
    encoded_return_to="$(_lh_hosted_urlencode "$safe_return_to")" || return 1
    url="${url}&return_to=${encoded_return_to}"
  fi

  printf '%s\n' "$url"
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
        clean(payload.get("data_path")),
        clean(payload.get("password")),
    ]
))
PY
}

_lh_hosted_parse_health_commit() {
  local response_file="$1"
  local python_bin
  python_bin="$(_lh_hosted_python_bin)" || return 1

  "$python_bin" - "$response_file" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], encoding="utf-8") as handle:
        payload = json.load(handle)
except Exception:
    print("", end="")
    raise SystemExit(0)

build = payload.get("build") or {}
print(str(build.get("commit") or build.get("commit_short") or ""), end="")
PY
}

_lh_hosted_image_tag() {
  local image="$1"
  local tag="${image##*:}"

  if [[ -z "$image" || "$tag" == "$image" || "$image" == *@* ]]; then
    printf ''
    return 0
  fi

  printf '%s' "$tag"
}

_lh_hosted_commit_matches_image_tag() {
  local commit="$1"
  local tag="$2"

  if [[ -z "$tag" || "$tag" == "latest" ]]; then
    return 0
  fi

  [[ -n "$commit" && ( "$commit" == "$tag"* || "$tag" == "$commit"* ) ]]
}

_lh_hosted_reprovision_api_url() {
  local subdomain="${LH_TARGET_SUBDOMAIN:-${LH_INSTANCE_SUBDOMAIN:-${INSTANCE_SUBDOMAIN:-}}}"

  if [[ -n "${LH_TARGET_API_URL:-}" ]]; then
    printf '%s\n' "$LH_TARGET_API_URL"
  elif [[ -n "${LH_INSTANCE_URL:-}" ]]; then
    printf '%s\n' "$LH_INSTANCE_URL"
  elif [[ -n "${API_URL:-}" ]]; then
    printf '%s\n' "$API_URL"
  elif [[ -n "${INSTANCE_URL:-}" ]]; then
    printf '%s\n' "$INSTANCE_URL"
  elif [[ -n "$subdomain" ]]; then
    printf 'https://%s.longhouse.ai\n' "$subdomain"
  fi
}

_lh_hosted_wait_for_runtime_image() {
  local api_url="$1"
  local image="$2"
  local timeout="${3:-240}"
  local expected_tag=""
  local deadline=0
  local response_file=""
  local http_code=""
  local commit=""
  local last_error=""

  if [[ -z "$api_url" ]]; then
    echo "Cannot poll reprovision result without an instance API URL" >&2
    return 1
  fi

  expected_tag="$(_lh_hosted_image_tag "$image")"
  deadline=$(( $(date +%s) + timeout ))

  while [[ "$(date +%s)" -lt "$deadline" ]]; do
    response_file="$(mktemp)"
    if ! http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
      --connect-timeout 5 --max-time 10 \
      "${api_url%/}/api/health")"; then
      http_code="000"
    fi

    if [[ "$http_code" == "200" ]]; then
      commit="$(_lh_hosted_parse_health_commit "$response_file")"
      if _lh_hosted_commit_matches_image_tag "$commit" "$expected_tag"; then
        rm -f "$response_file"
        return 0
      fi
      last_error="healthy commit=${commit:-unknown}, expected image tag=${expected_tag:-any}"
    else
      last_error="HTTP ${http_code}"
    fi

    rm -f "$response_file"
    sleep 3
  done

  echo "Timed out waiting for ${api_url%/}/api/health to report image ${image:-unknown}: ${last_error}" >&2
  return 1
}

lh_wait_for_runtime_image() {
  _lh_hosted_wait_for_runtime_image "$@"
}

_lh_hosted_export_instance_payload() {
  local parsed="$1"
  local fallback_subdomain="${2:-}"

  IFS=$'\t' read -r LH_INSTANCE_ID LH_INSTANCE_URL LH_INSTANCE_SUBDOMAIN LH_INSTANCE_STATUS LH_INSTANCE_CONTAINER_NAME LH_INSTANCE_DATA_PATH LH_INSTANCE_PASSWORD <<< "$parsed"
  if [[ -z "$LH_INSTANCE_SUBDOMAIN" && -n "$fallback_subdomain" ]]; then
    LH_INSTANCE_SUBDOMAIN="$fallback_subdomain"
  fi
  export LH_INSTANCE_ID LH_INSTANCE_URL LH_INSTANCE_SUBDOMAIN LH_INSTANCE_STATUS LH_INSTANCE_CONTAINER_NAME LH_INSTANCE_DATA_PATH LH_INSTANCE_PASSWORD
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
    status = instance.get("status")
    container_name = instance.get("container_name")
    data_path = instance.get("data_path")

    def clean(value):
        return str("" if value is None else value).replace("\t", " ").replace("\n", " ")

    print(
        "\t".join(
            [
                clean(instance_id),
                clean(url),
                clean(subdomain),
                clean(status),
                clean(container_name),
                clean(data_path),
            ]
        )
    )
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
  local attempt=1
  local max_attempts=5

  lh_hosted_prepare_control_plane_auth || return 1

  while [[ "$attempt" -le "$max_attempts" ]]; do
    response_file="$(mktemp)"
    if ! http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
      --connect-timeout 10 --max-time 30 \
      -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
      "${CONTROL_PLANE_URL%/}/api/instances")"; then
      http_code="000"
    fi

    if [[ "$http_code" == "200" ]]; then
      break
    fi

    if [[ "$attempt" -lt "$max_attempts" ]] && _lh_hosted_is_retryable_http_code "$http_code"; then
      echo "Transient control-plane instance lookup failure (HTTP ${http_code}); retrying (${attempt}/${max_attempts})..." >&2
      rm -f "$response_file"
      _lh_hosted_retry_sleep "$attempt"
      attempt=$((attempt + 1))
      continue
    fi

    echo "Failed to list control-plane instances (HTTP ${http_code})" >&2
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  done

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
  IFS=$'\t' read -r LH_INSTANCE_ID LH_INSTANCE_URL LH_INSTANCE_SUBDOMAIN LH_INSTANCE_STATUS LH_INSTANCE_CONTAINER_NAME LH_INSTANCE_DATA_PATH <<< "$parsed"
  export LH_INSTANCE_ID LH_INSTANCE_URL LH_INSTANCE_SUBDOMAIN LH_INSTANCE_STATUS LH_INSTANCE_CONTAINER_NAME LH_INSTANCE_DATA_PATH
}

lh_hosted_default_control_plane_url() {
  CONTROL_PLANE_URL="${CONTROL_PLANE_URL:-${CP_URL:-https://control.longhouse.ai}}"
  CP_URL="$CONTROL_PLANE_URL"
  export CONTROL_PLANE_URL CP_URL
}

lh_hosted_prepare_control_plane_auth() {
  lh_hosted_default_control_plane_url
  CONTROL_PLANE_ADMIN_TOKEN="${CONTROL_PLANE_ADMIN_TOKEN:-${ADMIN_TOKEN:-}}"

  # Auto-fetch from the control-plane container on zerg when running locally.
  # Silent no-op if SSH or the container is unavailable (e.g. CI with explicit token).
  if [[ -z "${CONTROL_PLANE_ADMIN_TOKEN:-}" ]] && command -v ssh &>/dev/null; then
    local _container
    _container="$(ssh -o ConnectTimeout=3 -o BatchMode=yes zerg \
      "docker ps --filter label=coolify.serviceName=longhouse-control-plane --format '{{.Names}}' | head -1" 2>/dev/null || true)"
    if [[ -n "$_container" ]]; then
      CONTROL_PLANE_ADMIN_TOKEN="$(ssh -o ConnectTimeout=3 -o BatchMode=yes zerg \
        "docker exec $_container python -c 'from control_plane.config import settings; print(settings.admin_token)'" 2>/dev/null || true)"
    fi
  fi

  export CONTROL_PLANE_ADMIN_TOKEN

  if ! lh_hosted_require_env CONTROL_PLANE_URL CONTROL_PLANE_ADMIN_TOKEN; then
    echo "Set CONTROL_PLANE_ADMIN_TOKEN or ADMIN_TOKEN before using hosted control-plane helpers. Secret loading is intentionally external so Longhouse stays provider-agnostic." >&2
    return 1
  fi
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
    echo "Set INSTANCE_SUBDOMAIN + CONTROL_PLANE_* or FRONTEND_URL/API_URL before preparing hosted target. Secret sourcing stays outside the repo so operators can use any manager they want." >&2
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

_lh_hosted_parse_access_token() {
  local response_file="$1"
  local python_bin
  python_bin="$(_lh_hosted_python_bin)" || return 1

  "$python_bin" - "$response_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

token = payload.get("access_token")
if not token:
    sys.exit(1)
print(token)
PY
}

_lh_hosted_parse_device_token_payload() {
  local response_file="$1"
  local python_bin
  python_bin="$(_lh_hosted_python_bin)" || return 1

  "$python_bin" - "$response_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)

token_id = payload.get("id")
token = payload.get("token")
if not token_id or not token:
    sys.exit(1)
print(f"{token_id}\t{token}")
PY
}

lh_hosted_issue_login_token() {
  local instance_id="${1:-${LH_INSTANCE_ID:-}}"
  local response_file=""
  local http_code=""
  local token=""
  local attempt=1
  local max_attempts=5

  if [[ -z "$instance_id" ]]; then
    echo "Missing instance id for login-token request" >&2
    return 1
  fi

  lh_hosted_prepare_control_plane_auth || return 1

  while [[ "$attempt" -le "$max_attempts" ]]; do
    response_file="$(mktemp)"
    if ! http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
      --connect-timeout 10 --max-time 30 \
      -X POST \
      -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
      "${CONTROL_PLANE_URL%/}/api/instances/${instance_id}/login-token")"; then
      http_code="000"
    fi

    if [[ "$http_code" == "200" ]]; then
      break
    fi

    if [[ "$attempt" -lt "$max_attempts" ]] && _lh_hosted_is_retryable_http_code "$http_code"; then
      echo "Transient hosted login-token failure (HTTP ${http_code}); retrying (${attempt}/${max_attempts})..." >&2
      rm -f "$response_file"
      _lh_hosted_retry_sleep "$attempt"
      attempt=$((attempt + 1))
      continue
    fi

    echo "Failed to issue login token for instance ${instance_id} (HTTP ${http_code})" >&2
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  done

  token="$(_lh_hosted_parse_token "$response_file")" || {
    echo "Login-token response missing token for instance ${instance_id}" >&2
    rm -f "$response_file"
    return 1
  }
  rm -f "$response_file"
  printf '%s\n' "$token"
}

lh_hosted_exchange_login_token() {
  local token="$1"
  local instance_url="${2:-${LH_INSTANCE_URL:-}}"
  local response_file=""
  local http_code=""
  local access_token=""
  local payload=""
  local attempt=1
  local max_attempts=5

  if [[ -z "$token" || -z "$instance_url" ]]; then
    echo "Usage: lh_hosted_exchange_login_token <token> [instance_url]" >&2
    return 1
  fi

  payload="$(_lh_hosted_json_object token "$token")" || return 1
  while [[ "$attempt" -le "$max_attempts" ]]; do
    response_file="$(mktemp)"
    if ! http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
      --connect-timeout 10 --max-time 30 \
      -H "Content-Type: application/json" \
      -d "$payload" \
      "${instance_url%/}/api/auth/accept-token")"; then
      http_code="000"
    fi

    if [[ "$http_code" == "200" ]]; then
      break
    fi

    if [[ "$attempt" -lt "$max_attempts" ]] && _lh_hosted_is_retryable_http_code "$http_code"; then
      echo "Transient accept-token failure at ${instance_url} (HTTP ${http_code}); retrying (${attempt}/${max_attempts})..." >&2
      rm -f "$response_file"
      _lh_hosted_retry_sleep "$attempt"
      attempt=$((attempt + 1))
      continue
    fi

    echo "Instance rejected login token at ${instance_url} (HTTP ${http_code})" >&2
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  done

  access_token="$(_lh_hosted_parse_access_token "$response_file")" || {
    echo "Accept-token response missing access_token for instance ${instance_url}" >&2
    rm -f "$response_file"
    return 1
  }

  rm -f "$response_file"
  printf '%s\n' "$access_token"
}

lh_hosted_create_device_token() {
  local access_token="$1"
  local api_url="$2"
  local device_id="${3:-hosted-smoke-$(date +%Y%m%d-%H%M%S)-$RANDOM}"
  local response_file=""
  local http_code=""
  local payload=""
  local parsed=""
  local attempt=1
  local max_attempts=5

  if [[ -z "$access_token" || -z "$api_url" ]]; then
    echo "Usage: lh_hosted_create_device_token <access_token> <api_url> [device_id]" >&2
    return 1
  fi

  payload="$(_lh_hosted_json_object device_id "$device_id")" || return 1
  while [[ "$attempt" -le "$max_attempts" ]]; do
    response_file="$(mktemp)"
    if ! http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
      --connect-timeout 10 --max-time 30 \
      -X POST \
      -H "Authorization: Bearer ${access_token}" \
      -H "Content-Type: application/json" \
      -d "$payload" \
      "${api_url%/}/api/devices/tokens")"; then
      http_code="000"
    fi

    if [[ "$http_code" == "200" || "$http_code" == "201" ]]; then
      break
    fi

    if [[ "$attempt" -lt "$max_attempts" ]] && _lh_hosted_is_retryable_http_code "$http_code"; then
      echo "Transient device-token creation failure at ${api_url} (HTTP ${http_code}); retrying (${attempt}/${max_attempts})..." >&2
      rm -f "$response_file"
      _lh_hosted_retry_sleep "$attempt"
      attempt=$((attempt + 1))
      continue
    fi

    echo "Failed to create device token at ${api_url} (HTTP ${http_code})" >&2
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  done

  parsed="$(_lh_hosted_parse_device_token_payload "$response_file")" || {
    echo "Device-token response missing id/token for ${device_id}" >&2
    rm -f "$response_file"
    return 1
  }

  rm -f "$response_file"
  printf '%s\n' "$parsed"
}

lh_hosted_revoke_device_token() {
  local access_token="$1"
  local token_id="$2"
  local api_url="$3"
  local response_file=""
  local http_code=""

  if [[ -z "$access_token" || -z "$token_id" || -z "$api_url" ]]; then
    echo "Usage: lh_hosted_revoke_device_token <access_token> <token_id> <api_url>" >&2
    return 1
  fi

  response_file="$(mktemp)"
  http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
    -X DELETE \
    -H "Authorization: Bearer ${access_token}" \
    "${api_url%/}/api/devices/tokens/${token_id}")"

  if [[ "$http_code" != "200" && "$http_code" != "204" ]]; then
    echo "Failed to revoke device token ${token_id} at ${api_url} (HTTP ${http_code})" >&2
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  fi

  rm -f "$response_file"
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

lh_hosted_accept_login_token_redirect() {
  local token="$1"
  local cookie_jar="$2"
  local return_to="${3:-}"
  local instance_url="${4:-${LH_INSTANCE_URL:-}}"
  local headers_file=""
  local http_code=""
  local location=""
  local request_url=""

  if [[ -z "$token" || -z "$cookie_jar" || -z "$instance_url" ]]; then
    echo "Usage: lh_hosted_accept_login_token_redirect <token> <cookie_jar> [return_to] [instance_url]" >&2
    return 1
  fi

  request_url="$(_lh_hosted_build_accept_token_redirect_url "$token" "$return_to" "$instance_url")" || return 1
  headers_file="$(mktemp)"
  http_code="$(curl -sS -D "$headers_file" -o /dev/null -w "%{http_code}" -c "$cookie_jar" "$request_url")"

  if [[ "$http_code" != "302" ]]; then
    echo "Instance rejected redirect login token at ${instance_url} (HTTP ${http_code})" >&2
    rm -f "$headers_file"
    return 1
  fi

  location="$(awk 'BEGIN{IGNORECASE=1} /^location:/ {sub(/\r$/, "", $2); print $2; exit}' "$headers_file")"
  rm -f "$headers_file"

  if [[ -z "$location" ]]; then
    echo "Accept-token redirect response missing Location header" >&2
    return 1
  fi

  printf '%s\n' "$location"
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
  local payload="${3:-}"
  local response_file=""
  local http_code=""
  LH_HOSTED_LAST_HTTP_CODE=""

  if [[ -z "$instance_id" ]]; then
    echo "Missing instance id for ${action} request" >&2
    return 1
  fi

  lh_hosted_prepare_control_plane_auth || return 1

  response_file="$(mktemp)"
  if [[ -n "$payload" ]]; then
    http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
      --connect-timeout 10 --max-time "${LH_HOSTED_ACTION_MAX_TIME:-75}" \
      -X POST \
      -H "Content-Type: application/json" \
      -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
      -d "$payload" \
      "${CONTROL_PLANE_URL%/}/api/instances/${instance_id}/${action}")"
  else
    http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
      --connect-timeout 10 --max-time "${LH_HOSTED_ACTION_MAX_TIME:-75}" \
      -X POST \
      -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
      "${CONTROL_PLANE_URL%/}/api/instances/${instance_id}/${action}")"
  fi
  LH_HOSTED_LAST_HTTP_CODE="$http_code"
  export LH_HOSTED_LAST_HTTP_CODE

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
  local image="${2:-}"
  local payload=""
  local api_url=""
  local http_code=""

  if [[ -n "$image" ]]; then
    payload="$(_lh_hosted_json_object image "$image")" || return 1
  fi

  if _lh_hosted_post_instance_action "$instance_id" "reprovision" "$payload"; then
    if [[ -n "$image" ]]; then
      api_url="$(_lh_hosted_reprovision_api_url)"
      echo "Waiting for hosted runtime health to report the requested image..." >&2
      _lh_hosted_wait_for_runtime_image "$api_url" "$image" 240
      return $?
    fi
    return 0
  fi

  http_code="${LH_HOSTED_LAST_HTTP_CODE:-}"
  if [[ "$http_code" != "524" && "$http_code" != "000" ]]; then
    return 1
  fi

  api_url="$(_lh_hosted_reprovision_api_url)"
  echo "Reprovision returned HTTP ${http_code}; polling hosted runtime health for the requested image..." >&2
  _lh_hosted_wait_for_runtime_image "$api_url" "$image" 240
}

lh_hosted_deprovision() {
  local instance_id="${1:-${LH_INSTANCE_ID:-}}"
  _lh_hosted_post_instance_action "$instance_id" "deprovision"
}
