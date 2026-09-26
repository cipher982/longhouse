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

_lh_hosted_parse_deployment_status() {
  local response_file="$1"
  local expected_target="${2:-}"
  local python_bin
  python_bin="$(_lh_hosted_python_bin)" || return 1
  "$python_bin" - "$response_file" "$expected_target" <<'PY'
import json
import sys

response_file, expected_target = sys.argv[1], sys.argv[2]
with open(response_file, encoding="utf-8") as handle:
    payload = json.load(handle)
deployment_id = payload.get("id")
if not deployment_id:
    raise SystemExit(3)
target_found = "unchecked"
target_state = ""
if expected_target:
    target_found = "no"
    for target in payload.get("targets") or []:
        if isinstance(target, dict) and str(target.get("id")) == expected_target:
            target_found = "yes"
            target_state = str(target.get("deploy_state") or "")
            break
# Tab-delimited, with a placeholder for an absent value: `read` with a tab IFS
# collapses adjacent empty fields, which silently shifted every later field by
# one and made a successful deployment look like a digest mismatch.
_EMPTY = "__LH_EMPTY__"
print(
    "\t".join(
        str(value) if value else _EMPTY
        for value in (
            deployment_id,
            payload.get("status"),
            payload.get("image"),
            payload.get("image_digest"),
            target_found,
            target_state,
        )
    )
)
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
  local expected_image="${3:-}"
  local expected_target="${4:-}"
  local deadline=$(( $(date +%s) + timeout ))
  local response_file=""
  local http_code=""
  local parsed=""
  local receipt_id=""
  local state=""
  local receipt_image=""
  local receipt_digest=""
  local target_found=""
  local target_state=""
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
      parsed="$(_lh_hosted_parse_deployment_status "$response_file" "$expected_target")" || {
        echo "Deployment ${deployment_id} response was missing a durable receipt id" >&2
        rm -f "$response_file"
        return 1
      }
      rm -f "$response_file"
      IFS=$'\t' read -r receipt_id state receipt_image receipt_digest target_found target_state <<< "$parsed"
      # The parser writes __LH_EMPTY__ where a value is absent; empty is the
      # meaning the callers below test for.
      for _field in receipt_image receipt_digest target_state; do
        if [[ "${!_field}" == "__LH_EMPTY__" ]]; then
          printf -v "$_field" '%s' ""
        fi
      done
      if [[ "$target_found" == "__LH_EMPTY__" ]]; then
        target_found=""
      fi
      LH_DEPLOYMENT_STATUS="$state"
      LH_DEPLOYMENT_IMAGE="$receipt_image"
      LH_DEPLOYMENT_IMAGE_DIGEST="$receipt_digest"
      LH_DEPLOYMENT_TARGET_ID="$expected_target"
      LH_DEPLOYMENT_TARGET_STATE="$target_state"
      export LH_DEPLOYMENT_STATUS LH_DEPLOYMENT_IMAGE LH_DEPLOYMENT_IMAGE_DIGEST
      export LH_DEPLOYMENT_TARGET_ID LH_DEPLOYMENT_TARGET_STATE

      if [[ "$receipt_id" != "$deployment_id" ]]; then
        echo "Deployment observer returned receipt ${receipt_id}, expected ${deployment_id}." >&2
        return 1
      fi
      case "$state" in
        superseded)
          echo "Deployment ${deployment_id} was superseded." >&2
          return 3
          ;;
        failure|failed|paused|rolled_back)
          echo "Deployment ${deployment_id} ended ${state}." >&2
          return 1
          ;;
      esac

      if [[ -n "$expected_target" && "$target_found" == "yes" ]]; then
        case "$target_state" in
          superseded)
            echo "Deployment ${deployment_id} target ${expected_target} was superseded." >&2
            return 3
            ;;
          failure|failed|paused|rolled_back)
            echo "Deployment ${deployment_id} target ${expected_target} ended ${target_state}." >&2
            return 1
            ;;
        esac
      fi

      if [[ -n "$expected_image" && -n "$receipt_image" && "$receipt_image" != "$expected_image" ]]; then
        echo "Deployment ${deployment_id} receipt image ${receipt_image} does not match requested ${expected_image}." >&2
        return 1
      fi
      if [[ -n "$expected_image" && -n "$receipt_digest" && "$receipt_digest" != "$expected_image" ]]; then
        echo "Deployment ${deployment_id} receipt digest ${receipt_digest} does not match requested ${expected_image}." >&2
        return 1
      fi

      case "$state" in
        success|completed)
          if [[ -n "$expected_image" ]]; then
            if [[ "$receipt_image" != "$expected_image" || "$receipt_digest" != "$expected_image" ]]; then
              echo "Deployment ${deployment_id} succeeded without the requested image identity." >&2
              return 1
            fi
          fi
          if [[ -n "$expected_target" ]]; then
            if [[ "$target_found" != "yes" ]]; then
              echo "Deployment ${deployment_id} succeeded without requested target ${expected_target}." >&2
              return 1
            fi
            if [[ "$target_state" != "success" ]]; then
              echo "Deployment ${deployment_id} target ${expected_target} ended ${target_state:-unknown}, not success." >&2
              return 1
            fi
          fi
          return 0
          ;;
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

_lh_hosted_deployment_payload() {
  local image="$1"
  local target_ids_json="$2"
  local production="$3"
  local max_parallel="${4:-}"
  local failure_threshold="${5:-}"
  local python_bin
  python_bin="$(_lh_hosted_python_bin)" || return 1
  "$python_bin" - "$image" "$target_ids_json" "$production" "$max_parallel" "$failure_threshold" \
    "${LH_DEPLOYMENT_SOURCE_SHA}" \
    "${LH_DEPLOYMENT_BUILD_IDENTITY}" \
    "${LH_DEPLOYMENT_SOURCE_WORKFLOW}" \
    "${LH_DEPLOYMENT_SOURCE_ORDER}" \
    "${LH_DEPLOYMENT_QUALIFICATION_ID}" \
    "${LH_DEPLOYMENT_SCHEMA_VERSION}" \
    "${LH_DEPLOYMENT_SCHEMA_MIN_READER}" \
    "${LH_DEPLOYMENT_SCHEMA_MAX_READER}" \
    "${LH_DEPLOYMENT_REASON:-hosted release}" <<'PY'
import json
import sys

image, target_ids_json, production, max_parallel, failure_threshold, source_sha, build_identity, workflow, source_order, qualification_id, schema, minimum, maximum, reason = sys.argv[1:]
payload = {
    "image": image,
    # A JSON array, possibly empty. An explicit empty list (not an omitted
    # field) is what tells the control plane a production promotion has zero
    # targets and is a pointer-only promotion (release-rings.md change 3).
    "target_instance_ids": json.loads(target_ids_json),
    "source_sha": source_sha,
    "build_identity": build_identity,
    "source_workflow": workflow,
    "source_order": int(source_order),
    "qualification_id": qualification_id,
    "ready": True,
    "schema_version": int(schema),
    "schema_min_reader": int(minimum),
    "schema_max_reader": int(maximum),
    "reason": reason,
    # Only an explicit operator promotion advances the new-tenant default image.
    "production_promotion": production == "1",
}
if max_parallel:
    payload["max_parallel"] = int(max_parallel)
if failure_threshold:
    payload["failure_threshold"] = int(failure_threshold)
print(json.dumps(payload, separators=(",", ":")), end="")
PY
}

_lh_hosted_reprovision_payload() {
  local instance_id="$1"
  local image="$2"
  _lh_hosted_deployment_payload "$image" "[$instance_id]" "${LH_DEPLOYMENT_PRODUCTION_PROMOTION:-0}"
}


_lh_hosted_resolve_build_identity() {
  local image="$1"
  local identity=""
  if [[ -n "${LH_DEPLOYMENT_BUILD_IDENTITY:-}" ]]; then
    return 0
  fi
  if [[ -n "${REGISTRY_USERNAME:-}" && -n "${REGISTRY_PASSWORD:-}" ]]; then
    printf '%s' "$REGISTRY_PASSWORD" | docker login ghcr.io \
      --username "$REGISTRY_USERNAME" --password-stdin >/dev/null
  fi
  identity="$(
    IMAGE_REF="$image" IDENTITY_TIMEOUT_SECONDS="${LH_HOSTED_BUILD_IDENTITY_TIMEOUT_SECONDS:-180}" \
      python3 - <<'PY'
import os
import signal
import subprocess
import uuid

def terminate(signum, _frame):
    raise SystemExit(128 + signum)

signal.signal(signal.SIGTERM, terminate)
container_name = "longhouse-image-identity-" + uuid.uuid4().hex

try:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--name",
            container_name,
            "--pull=always",
            "--platform",
            "linux/amd64",
            "--network",
            "none",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges:true",
            "--entrypoint",
            "/bin/cat",
            os.environ["IMAGE_REF"],
            "/app/zerg/build_identity.json",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=float(os.environ["IDENTITY_TIMEOUT_SECONDS"]),
    )
except subprocess.TimeoutExpired as exc:
    raise SystemExit(f"timed out reading published build identity: {exc}") from exc
finally:
    subprocess.run(
        ["docker", "rm", "--force", container_name],
        capture_output=True, text=True, timeout=30,
    )
    remaining = subprocess.run(
        ["docker", "container", "ls", "--all", "--filter", f"name=^/{container_name}$", "--format", "{{.ID}}"],
        check=True, capture_output=True, text=True, timeout=30,
    )
    if remaining.stdout.strip():
        raise RuntimeError(f"build identity container cleanup failed: {container_name}")
print(result.stdout, end="")
PY
  )" || {
    echo "Unable to read the selected image's published build identity; refusing deployment without immutable identity evidence." >&2
    return 1
  }
  LH_DEPLOYMENT_BUILD_IDENTITY="$identity"
  export LH_DEPLOYMENT_BUILD_IDENTITY
}


_lh_hosted_normalize_build_identity() {
  local source_sha="$1"
  local build_identity="$2"
  BUILD_IDENTITY="$build_identity" SOURCE_SHA="$source_sha" python3 - <<'PY'
import json
import os

source_sha = os.environ["SOURCE_SHA"]
try:
    identity = json.loads(os.environ["BUILD_IDENTITY"])
except json.JSONDecodeError as exc:
    raise SystemExit(f"LH_DEPLOYMENT_BUILD_IDENTITY is not a JSON object: {exc}")
if not isinstance(identity, dict) or not identity:
    raise SystemExit("LH_DEPLOYMENT_BUILD_IDENTITY must be a non-empty JSON object")
required = ("built_at", "channel", "commit", "commit_short", "dirty", "version")
missing = [key for key in required if key not in identity]
if missing:
    raise SystemExit(f"LH_DEPLOYMENT_BUILD_IDENTITY is missing normalized fields: {', '.join(missing)}")
if identity["commit"] != source_sha or identity["commit_short"] != source_sha[:8]:
    raise SystemExit("LH_DEPLOYMENT_BUILD_IDENTITY commit does not match the selected image source SHA")
if identity.get("source_sha") is not None and identity["source_sha"] != source_sha:
    raise SystemExit("LH_DEPLOYMENT_BUILD_IDENTITY.source_sha does not match the selected image source SHA")
if not isinstance(identity["built_at"], str) or not identity["built_at"]:
    raise SystemExit("LH_DEPLOYMENT_BUILD_IDENTITY.built_at must be a non-empty string")
if not isinstance(identity["channel"], str) or not identity["channel"]:
    raise SystemExit("LH_DEPLOYMENT_BUILD_IDENTITY.channel must be a non-empty string")
if not isinstance(identity["dirty"], bool):
    raise SystemExit("LH_DEPLOYMENT_BUILD_IDENTITY.dirty must be boolean")
if not isinstance(identity["version"], str) or not identity["version"]:
    raise SystemExit("LH_DEPLOYMENT_BUILD_IDENTITY.version must be a non-empty string")
print(json.dumps({key: identity[key] for key in required}, separators=(",", ":"), sort_keys=True))
PY
}


_lh_hosted_require_deployment_provenance() {
  local source_sha="${LH_DEPLOYMENT_SOURCE_SHA:-}"
  local build_identity="${LH_DEPLOYMENT_BUILD_IDENTITY:-}"
  local workflow="${LH_DEPLOYMENT_SOURCE_WORKFLOW:-}"
  local source_order="${LH_DEPLOYMENT_SOURCE_ORDER:-}"
  local qualification_id="${LH_DEPLOYMENT_QUALIFICATION_ID:-}"
  if [[ ! "$source_sha" =~ ^[0-9a-f]{40}$ ]]; then
    echo "Missing exact selected-image LH_DEPLOYMENT_SOURCE_SHA; refusing deployment without immutable source provenance." >&2
    return 1
  fi
  if [[ -z "$build_identity" ]]; then
    echo "Missing published image build identity; refusing deployment without immutable identity evidence." >&2
    return 1
  fi
  build_identity="$(_lh_hosted_normalize_build_identity "$source_sha" "$build_identity")" || return 1
  if [[ -z "$workflow" ]]; then
    echo "Missing LH_DEPLOYMENT_SOURCE_WORKFLOW; refusing deployment without publication provenance." >&2
    return 1
  fi
  if [[ ! "$source_order" =~ ^[1-9][0-9]*$ ]]; then
    echo "Missing exact LH_DEPLOYMENT_SOURCE_ORDER; refusing deployment without publication provenance." >&2
    return 1
  fi
  if [[ -z "$qualification_id" || "${#qualification_id}" -gt 128 ]]; then
    echo "LH_DEPLOYMENT_QUALIFICATION_ID must be non-empty and at most 128 characters." >&2
    return 1
  fi
  export LH_DEPLOYMENT_BUILD_IDENTITY="$build_identity"
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
  if (( LH_DEPLOYMENT_SCHEMA_MIN_READER > LH_DEPLOYMENT_SCHEMA_MAX_READER )); then
    echo "Selected image schema reader bounds are invalid: minimum exceeds maximum." >&2
    return 1
  fi
}

lh_hosted_reprovision() {
  local instance_id="${1:-${LH_INSTANCE_ID:-}}"
  local image="${2:-}"
  local payload=""
  local key="${LH_DEPLOYMENT_IDEMPOTENCY_KEY:-${RUNTIME_DEPLOYMENT_IDEMPOTENCY_KEY:-}}"
  if [[ -z "$instance_id" || -z "$image" ]]; then
    echo "Usage: lh_hosted_reprovision <instance-id> <immutable-image>" >&2
    return 1
  fi
  if [[ ! "$image" =~ @sha256:[0-9a-f]{64}$ ]]; then
    echo "Refusing non-immutable deployment image; resolve a sha256 digest first." >&2
    return 1
  fi
  if [[ -z "$key" ]]; then
    echo "Missing deployment idempotency key; refusing an untracked deployment submission." >&2
    return 1
  fi
  _lh_hosted_resolve_image_metadata "$image" || return 1
  _lh_hosted_require_schema_metadata || return 1
  _lh_hosted_resolve_build_identity "$image" || return 1
  _lh_hosted_require_deployment_provenance || return 1
  payload="$(_lh_hosted_reprovision_payload "$instance_id" "$image")" || return 1
  lh_hosted_submit_deployment "$payload" "$key" || return 1
  echo "Submitted durable deployment ${LH_DEPLOYMENT_ID} for instance ${instance_id}." >&2
  lh_hosted_wait_for_deployment "$LH_DEPLOYMENT_ID" "${LH_HOSTED_REPROVISION_TIMEOUT:-900}" "$image" "$instance_id"
}

# Production promotion (release-rings.md change 2): unlike lh_hosted_reprovision,
# the target set is zero-to-many explicit instance ids (a JSON array, possibly
# "[]" for a pointer-only promotion) rather than exactly one, so there is no
# single expected_target to verify against -- callers rely on the deployment's
# own aggregate status instead. Provenance (source_sha, schema, build identity)
# is resolved the same way lh_hosted_reprovision resolves it: by inspecting the
# selected image directly, never by trusting caller-supplied claims about it.
lh_hosted_reprovision_production() {
  local image="${1:-}"
  local target_ids_json="${2:-}"
  local timeout="${3:-${LH_HOSTED_REPROVISION_TIMEOUT:-1800}}"
  local payload=""
  local key="${LH_DEPLOYMENT_IDEMPOTENCY_KEY:-}"
  if [[ -z "$image" || -z "$target_ids_json" ]]; then
    echo "Usage: lh_hosted_reprovision_production <immutable-image> <target-ids-json> [timeout]" >&2
    return 1
  fi
  if [[ ! "$image" =~ @sha256:[0-9a-f]{64}$ ]]; then
    echo "Refusing non-immutable deployment image; resolve a sha256 digest first." >&2
    return 1
  fi
  if [[ -z "$key" ]]; then
    echo "Missing deployment idempotency key; refusing an untracked deployment submission." >&2
    return 1
  fi
  _lh_hosted_resolve_image_metadata "$image" || return 1
  _lh_hosted_require_schema_metadata || return 1
  _lh_hosted_resolve_build_identity "$image" || return 1
  _lh_hosted_require_deployment_provenance || return 1
  payload="$(_lh_hosted_deployment_payload "$image" "$target_ids_json" 1 1 1)" || return 1
  lh_hosted_submit_deployment "$payload" "$key" || return 1
  echo "Submitted durable production deployment ${LH_DEPLOYMENT_ID}." >&2
  lh_hosted_wait_for_deployment "$LH_DEPLOYMENT_ID" "$timeout" "$image" ""
}

lh_hosted_deprovision() {
  local instance_id="${1:-${LH_INSTANCE_ID:-}}"
  _lh_hosted_post_instance_action "$instance_id" "deprovision"
}
