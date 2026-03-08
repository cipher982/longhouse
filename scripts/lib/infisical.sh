#!/usr/bin/env bash

LH_INFISICAL_PERSONAL_SHELL_PROJECT_ID="${LH_INFISICAL_PERSONAL_SHELL_PROJECT_ID:-a3f40ca4-1a1f-4499-be6b-8a4e96b3a3cf}"
LH_INFISICAL_OPS_INFRA_PROJECT_ID="${LH_INFISICAL_OPS_INFRA_PROJECT_ID:-d303262d-e281-4100-aba7-28940cf2741e}"
LH_INFISICAL_DOMAIN="${LH_INFISICAL_DOMAIN:-${INFISICAL_DOMAIN:-https://secrets.drose.io}}"

_lh_infisical_require_var_name() {
  local var_name="$1"
  if [[ ! "$var_name" =~ ^[A-Z_][A-Z0-9_]*$ ]]; then
    echo "Invalid shell variable name: ${var_name}" >&2
    return 1
  fi
}

lh_infisical_default_project_id() {
  local secret_key="$1"
  case "$secret_key" in
    CONTROL_PLANE_ADMIN_TOKEN)
      printf '%s\n' "${LH_INFISICAL_PROJECT_ID:-$LH_INFISICAL_OPS_INFRA_PROJECT_ID}"
      ;;
    *)
      printf '%s\n' "${LH_INFISICAL_PROJECT_ID:-$LH_INFISICAL_PERSONAL_SHELL_PROJECT_ID}"
      ;;
  esac
}

lh_infisical_default_env() {
  local secret_key="$1"
  case "$secret_key" in
    CONTROL_PLANE_ADMIN_TOKEN)
      printf '%s\n' "${LH_INFISICAL_ENV:-prod}"
      ;;
    *)
      printf '%s\n' "${LH_INFISICAL_ENV:-dev}"
      ;;
  esac
}

_lh_infisical_helper_bin() {
  if [[ -n "${LH_INFISICAL_GET_BIN:-}" && -x "${LH_INFISICAL_GET_BIN}" ]]; then
    printf '%s\n' "${LH_INFISICAL_GET_BIN}"
    return 0
  fi

  local candidate="$HOME/git/me/scripts/infisical-get.py"
  if [[ -x "$candidate" ]]; then
    printf '%s\n' "$candidate"
    return 0
  fi

  return 1
}

_lh_infisical_python_bin() {
  if command -v python3 >/dev/null 2>&1; then
    printf '%s\n' python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    printf '%s\n' python
    return 0
  fi
  echo "Missing python3/python for Infisical helper" >&2
  return 1
}

_lh_infisical_cli_fallback_get() {
  local secret_key="$1"
  local project_id="$2"
  local env_name="$3"
  local secret_path="$4"
  local domain="$5"
  local python_bin=""
  local raw_json=""

  if ! command -v infisical >/dev/null 2>&1; then
    echo "Missing infisical CLI. Install it or configure LH_INFISICAL_GET_BIN." >&2
    return 1
  fi

  python_bin="$(_lh_infisical_python_bin)" || return 1
  if ! raw_json="$(infisical secrets \
    --projectId "$project_id" \
    --env "$env_name" \
    --path "$secret_path" \
    --output json \
    --silent \
    --domain "$domain")"; then
    echo "Failed to read Infisical secret ${secret_key} from project ${project_id} env ${env_name}." >&2
    return 1
  fi

  if [[ -z "$raw_json" ]]; then
    echo "Infisical returned no secrets for project ${project_id} env ${env_name} path ${secret_path}." >&2
    return 1
  fi

  INFISICAL_RAW_JSON="$raw_json" "$python_bin" - "$secret_key" <<'PY'
import json
import os
import sys

secret_key = sys.argv[1]
raw_json = os.environ.get("INFISICAL_RAW_JSON", "")
if not raw_json:
    raise SystemExit("Failed to parse Infisical JSON output: empty response")

try:
    payload = json.loads(raw_json)
except UnicodeDecodeError as exc:
    raise SystemExit(f"Failed to decode Infisical JSON output: {exc}")
except json.JSONDecodeError as exc:
    raise SystemExit(f"Failed to parse Infisical JSON output: {exc}")

if not isinstance(payload, list):
    raise SystemExit("Unexpected Infisical JSON response shape")

for item in payload:
    if not isinstance(item, dict) or item.get("secretKey") != secret_key:
        continue
    value = item.get("secretValue")
    if value is None or value == "":
        raise SystemExit(f"Secret {secret_key} is missing or empty")
    print(value, end="")
    raise SystemExit(0)

raise SystemExit(f"Secret {secret_key} not found")
PY
}

lh_infisical_get_secret() {
  local secret_key="$1"
  local project_id="${2:-$(lh_infisical_default_project_id "$secret_key")}"
  local env_name="${3:-$(lh_infisical_default_env "$secret_key")}"
  local secret_path="${4:-/}"
  local domain="${5:-$LH_INFISICAL_DOMAIN}"
  local helper_bin=""

  if helper_bin="$(_lh_infisical_helper_bin)"; then
    "$helper_bin" "$secret_key" --project-id "$project_id" --env "$env_name" --path "$secret_path" --domain "$domain"
    return $?
  fi

  _lh_infisical_cli_fallback_get "$secret_key" "$project_id" "$env_name" "$secret_path" "$domain"
}

lh_infisical_export_secret_if_missing() {
  local var_name="$1"
  local secret_key="${2:-$var_name}"
  local project_id="${3:-$(lh_infisical_default_project_id "$secret_key")}"
  local env_name="${4:-$(lh_infisical_default_env "$secret_key")}"
  local secret_path="${5:-/}"
  local value=""

  _lh_infisical_require_var_name "$var_name" || return 1

  if [[ -n "${!var_name:-}" ]]; then
    return 0
  fi

  value="$(lh_infisical_get_secret "$secret_key" "$project_id" "$env_name" "$secret_path")" || return 1
  if [[ -z "$value" ]]; then
    echo "Infisical returned an empty value for ${secret_key}" >&2
    return 1
  fi

  printf -v "$var_name" '%s' "$value"
  export "$var_name"
}
