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



_lh_hosted_parse_deployment_payload() {
  local response_file="$1"
  local python_bin
  python_bin="$(_lh_hosted_python_bin)" || return 1
  "$python_bin" - "$response_file" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    payload = json.load(handle)
deployment_id = payload.get("id")
if not deployment_id:
    raise SystemExit(3)
print("\t".join(str(value or "") for value in (deployment_id, payload.get("status"), payload.get("image_digest"))))
PY
}

lh_hosted_submit_deployment() {
  local payload="${1:-}"
  local submission_key="${2:-}"
  local response_file=""
  local http_code=""
  local parsed=""
  local attempt=1
  local max_attempts="${LH_HOSTED_DEPLOYMENT_MAX_ATTEMPTS:-5}"

  if [[ -z "$payload" ]]; then
    echo "Usage: lh_hosted_submit_deployment <json-payload> [idempotency-key]" >&2
    return 1
  fi
  lh_hosted_prepare_control_plane_auth || return 1
  if [[ -z "$submission_key" ]]; then
    submission_key="hosted-deploy-$(printf '%s' "$payload" | shasum -a 256 | awk '{print $1}')"
  fi
  while [[ "$attempt" -le "$max_attempts" ]]; do
    response_file="$(mktemp)"
    if ! http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
      --connect-timeout 10 --max-time "${LH_HOSTED_DEPLOYMENT_MAX_TIME:-75}" \
      -X POST \
      -H "Content-Type: application/json" \
      -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
      -H "Idempotency-Key: ${submission_key}" \
      -d "$payload" \
      "${CONTROL_PLANE_URL%/}/api/deployments")"; then
      http_code="000"
    fi
    if [[ "$http_code" == "200" || "$http_code" == "201" ]]; then
      parsed="$(_lh_hosted_parse_deployment_payload "$response_file")" || {
        echo "Deployment submission response missing id" >&2
        rm -f "$response_file"
        return 1
      }
      rm -f "$response_file"
      IFS=$'\t' read -r LH_DEPLOYMENT_ID LH_DEPLOYMENT_STATUS LH_DEPLOYMENT_IMAGE_DIGEST <<< "$parsed"
      export LH_DEPLOYMENT_ID LH_DEPLOYMENT_STATUS LH_DEPLOYMENT_IMAGE_DIGEST
      return 0
    fi
    if [[ "$attempt" -lt "$max_attempts" ]] && _lh_hosted_is_retryable_http_code "$http_code"; then
      echo "Transient deployment submission failure (HTTP ${http_code}); retrying (${attempt}/${max_attempts})..." >&2
      rm -f "$response_file"
      _lh_hosted_retry_sleep "$attempt"
      attempt=$((attempt + 1))
      continue
    fi
    echo "Failed to submit deployment (HTTP ${http_code})" >&2
    cat "$response_file" >&2
    rm -f "$response_file"
    return 1
  done
}

lh_hosted_wait_for_deployment() {
  local deployment_id="${1:-${LH_DEPLOYMENT_ID:-}}"
  local timeout="${2:-900}"
  local deadline=$(( $(date +%s) + timeout ))
  local response_file=""
  local http_code=""
  local state=""
  local python_bin=""
  if [[ -z "$deployment_id" ]]; then
    echo "Missing deployment id to observe" >&2
    return 1
  fi
  lh_hosted_prepare_control_plane_auth || return 1
  while [[ "$(date +%s)" -lt "$deadline" ]]; do
    response_file="$(mktemp)"
    if ! http_code="$(curl -sS -o "$response_file" -w "%{http_code}" \
      --connect-timeout 10 --max-time 30 \
      -H "X-Admin-Token: ${CONTROL_PLANE_ADMIN_TOKEN}" \
      "${CONTROL_PLANE_URL%/}/api/deployments/${deployment_id}")"; then
      http_code="000"
    fi
    if [[ "$http_code" == "200" ]]; then
      python_bin="$(_lh_hosted_python_bin)" || return 1
      state="$("$python_bin" - "$response_file" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as handle:
    print(json.load(handle).get("status") or "")
PY
)"
      rm -f "$response_file"
      LH_DEPLOYMENT_STATUS="$state"
      export LH_DEPLOYMENT_STATUS
      case "$state" in
        success|completed) return 0 ;;
        superseded) echo "Deployment ${deployment_id} was superseded." >&2; return 3 ;;
        failure|failed|paused) echo "Deployment ${deployment_id} ended ${state}." >&2; return 1 ;;
      esac
    else
      rm -f "$response_file"
      if ! _lh_hosted_is_retryable_http_code "$http_code"; then
        echo "Failed to observe deployment ${deployment_id} (HTTP ${http_code})" >&2
        return 1
      fi
    fi
    sleep "${LH_HOSTED_DEPLOYMENT_POLL_SECONDS:-3}"
  done
  echo "Timed out waiting for deployment ${deployment_id}." >&2
  return 2
}

_lh_hosted_reprovision_payload() {
  local instance_id="$1"
  local image="$2"
  local python_bin
  python_bin="$(_lh_hosted_python_bin)" || return 1
  "$python_bin" - "$instance_id" "$image" \
    "${LH_DEPLOYMENT_SOURCE_SHA:-}" \
    "${LH_DEPLOYMENT_BUILD_IDENTITY:-}" \
    "${LH_DEPLOYMENT_SOURCE_WORKFLOW:-}" \
    "${LH_DEPLOYMENT_SOURCE_ORDER:-}" \
    "${LH_DEPLOYMENT_QUALIFICATION_ID:-}" \
    "${LH_DEPLOYMENT_SCHEMA_VERSION:-}" \
    "${LH_DEPLOYMENT_SCHEMA_MIN_READER:-}" \
    "${LH_DEPLOYMENT_SCHEMA_MAX_READER:-}" \
    "${LH_DEPLOYMENT_REASON:-hosted release}" <<'PY'
import json
import re
import sys

instance_id, image, source_sha, build_identity, workflow, source_order, qualification_id, schema, minimum, maximum, reason = sys.argv[1:]
payload = {
    "image": image,
    "target_instance_ids": [int(instance_id)],
    "reason": reason,
    "ready": True,
}
for key, value in (
    ("source_sha", source_sha),
    ("build_identity", build_identity),
    ("source_workflow", workflow),
    ("qualification_id", qualification_id),
    ("schema_version", schema),
    ("schema_min_reader", minimum),
    ("schema_max_reader", maximum),
):
    if value:
        payload[key] = value
if source_order:
    payload["source_order"] = int(source_order)
print(json.dumps(payload, separators=(",", ":")), end="")
PY
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

_lh_hosted_resolve_image_metadata() {
  local image="$1"
  local helper_root=""
  local inspector=""
  local metadata=""
  local resolved=""
  helper_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  inspector="$helper_root/scripts/ops/release-artifacts.py"
  if [[ ! -f "$inspector" ]]; then
    echo "Missing OCI metadata inspector; bootstrap historical images before deployment." >&2
    return 1
  fi
  metadata="$(python3 "$inspector" inspect --image "$image")" || {
    echo "Unable to inspect selected image metadata; bootstrap historical images with OCI source/schema labels before deployment." >&2
    return 1
  }
  resolved="$(
    SELECTED_METADATA="$metadata" \
    SELECTED_IMAGE="$image" \
    python3 - <<'PY'
import json
import os
import re

metadata = json.loads(os.environ["SELECTED_METADATA"])
image = os.environ["SELECTED_IMAGE"]
selected_digest = image.rsplit("@", 1)[-1]
if metadata.get("image_digest") != selected_digest:
    raise SystemExit("selected image metadata digest does not match the deployment digest")
source_sha = metadata.get("source_sha")
if not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-f]{40}", source_sha):
    raise SystemExit("selected image has no full source revision label; bootstrap historical images before deployment")
for field in ("schema_version", "schema_min_reader", "schema_max_reader"):
    value = metadata.get(field)
    if type(value) is not int or value < 0:
        raise SystemExit(f"selected image has no numeric {field}; bootstrap historical images before deployment")
expected_source = (
    os.environ.get("LH_DEPLOYMENT_SOURCE_SHA", "").strip().lower()
    or os.environ.get("RUNTIME_SOURCE_SHA", "").strip().lower()
)
if expected_source and expected_source != source_sha:
    raise SystemExit(f"deployment source {expected_source} does not match selected image source {source_sha}")
for env_names, field in (
    (("LH_DEPLOYMENT_SCHEMA_VERSION", "RUNTIME_SCHEMA_VERSION"), "schema_version"),
    (("LH_DEPLOYMENT_SCHEMA_MIN_READER", "RUNTIME_SCHEMA_MIN_READER"), "schema_min_reader"),
    (("LH_DEPLOYMENT_SCHEMA_MAX_READER", "RUNTIME_SCHEMA_MAX_READER"), "schema_max_reader"),
):
    expected = next((os.environ.get(name, "").strip() for name in env_names if os.environ.get(name, "").strip()), "")
    if expected and expected != str(metadata[field]):
        raise SystemExit(f"{env_names[0]} does not match selected image metadata")
print(source_sha, metadata["schema_version"], metadata["schema_min_reader"], metadata["schema_max_reader"])
PY
  )" || {
    echo "Selected image metadata does not match deployment metadata; refusing mixed digest/source/schema submission." >&2
    return 1
  }
  read -r LH_DEPLOYMENT_SOURCE_SHA LH_DEPLOYMENT_SCHEMA_VERSION LH_DEPLOYMENT_SCHEMA_MIN_READER LH_DEPLOYMENT_SCHEMA_MAX_READER <<< "$resolved"
  export LH_DEPLOYMENT_SOURCE_SHA LH_DEPLOYMENT_SCHEMA_VERSION LH_DEPLOYMENT_SCHEMA_MIN_READER LH_DEPLOYMENT_SCHEMA_MAX_READER
}

_lh_hosted_require_schema_metadata() {
  local name=""
  local value=""
  for name in LH_DEPLOYMENT_SCHEMA_VERSION LH_DEPLOYMENT_SCHEMA_MIN_READER LH_DEPLOYMENT_SCHEMA_MAX_READER; do
    value="${!name:-}"
    if [[ ! "$value" =~ ^[0-9]+$ ]]; then
      echo "Missing exact selected-image ${name}; bootstrap historical images with OCI schema labels before deployment." >&2
      return 1
    fi
  done
}

lh_hosted_reprovision() {
  local instance_id="${1:-${LH_INSTANCE_ID:-}}"
  local image="${2:-}"
  local payload=""
  local key="${LH_DEPLOYMENT_IDEMPOTENCY_KEY:-}"
  if [[ -z "$instance_id" || -z "$image" ]]; then
    echo "Usage: lh_hosted_reprovision <instance-id> <immutable-image>" >&2
    return 1
  fi
  if [[ ! "$image" =~ @sha256:[0-9a-f]{64}$ ]]; then
    echo "Refusing non-immutable deployment image; resolve a sha256 digest first." >&2
    return 1
  fi
  _lh_hosted_resolve_image_metadata "$image" || return 1
  _lh_hosted_require_schema_metadata || return 1
  payload="$(_lh_hosted_reprovision_payload "$instance_id" "$image")" || return 1
  lh_hosted_submit_deployment "$payload" "$key" || return 1
  echo "Submitted durable deployment ${LH_DEPLOYMENT_ID} for instance ${instance_id}." >&2
  lh_hosted_wait_for_deployment "$LH_DEPLOYMENT_ID" "${LH_HOSTED_REPROVISION_TIMEOUT:-900}"
}

lh_hosted_deprovision() {
  local instance_id="${1:-${LH_INSTANCE_ID:-}}"
  _lh_hosted_post_instance_action "$instance_id" "deprovision"
}
