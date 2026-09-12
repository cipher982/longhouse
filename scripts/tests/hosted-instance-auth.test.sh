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
trap 'rm -f "$temp_json" "$temp_json.request" "$temp_json.body" "$temp_json.attempts" "$temp_json.health-request"' EXIT

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

sleep() {
  :
}

printf '0' >"$temp_json.attempts"
curl() {
  local data=""
  local output_file=""
  local request_url=""
  local attempts=0
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
      attempts="$(cat "$temp_json.attempts")"
      attempts=$((attempts + 1))
      printf '%s' "$attempts" >"$temp_json.attempts"
      printf '%s' "$request_url" >"$temp_json.request"
      printf '%s' "$data" >"$temp_json.body"
      if [[ "$attempts" -eq 1 ]]; then
        printf 'instance locked' >"$output_file"
        printf '409'
      else
        printf '200'
      fi
      ;;
    */api/health)
      printf '%s' "$request_url" >"$temp_json.health-request"
      printf '{"build":{"commit":"facefeed"}}' >"$output_file"
      printf '200'
      ;;
    *)
      echo "Unexpected curl URL in reprovision lock retry test: $request_url" >&2
      return 1
      ;;
  esac
}

export LH_HOSTED_REPROVISION_MAX_ATTEMPTS=2
lh_hosted_reprovision "7" "ghcr.io/cipher982/longhouse-runtime:facefeed"
unset LH_HOSTED_REPROVISION_MAX_ATTEMPTS

if [[ "$(cat "$temp_json.attempts")" -ne 2 ]]; then
  echo "Expected reprovision helper to retry once after HTTP 409"
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

# Exercise the real entrypoints with a stale runner-local dotenv. The helper is
# deliberately absent in the scratch checkout: no network action can follow.
python3 - "$ROOT_DIR" <<'PY'
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

scripts = Path(sys.argv[1])
with tempfile.TemporaryDirectory(prefix="lh-ci-authority-") as directory:
    root = Path(directory)
    (root / ".env").write_text("echo STALE_AUTHORITY_LOADED >&2\nexit 93\n")
    for relative in (
        "ci/export-hosted-instance-env.sh",
        "qa/run-prod-e2e.sh",
        "qa/hosted-shipper-mixed-bench.sh",
        "qa/smoke-prod.sh",
        "qa/qa-live.sh",
        "qa/render-canary.sh",
    ):
        target = root / "scripts" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(scripts / relative, target)
        environment = {"PATH": os.environ["PATH"]}
        local = subprocess.run(
            ["bash", str(target)], env=environment, capture_output=True, timeout=10
        )
        assert local.returncode == 93, (relative, local.stderr)
        ci = subprocess.run(
            ["bash", str(target)], env={**environment, "CI": "true"},
            capture_output=True, timeout=10,
        )
        assert ci.returncode != 93, (relative, ci.stderr)
        assert b"STALE_AUTHORITY_LOADED" not in ci.stderr, (relative, ci.stderr)

    # Make runs before these scripts and must preserve the same authority.
    (root / ".env").write_text("CONTROL_PLANE_URL=stale-checkout\n")
    probe = root / "authority.mk"
    probe.write_text('authority-probe:\n\t@printf "%s\\n" "$$CONTROL_PLANE_URL"\n')
    command = [
        "make", "--no-print-directory", "-f", str(scripts.parent / "Makefile"),
        "-f", str(probe), "authority-probe",
    ]
    environment = {"PATH": os.environ["PATH"], "CONTROL_PLANE_URL": "workflow-authority"}
    local = subprocess.run(
        command, cwd=root, env=environment, capture_output=True, check=True, timeout=10
    )
    assert local.stdout.strip() == b"stale-checkout", local.stdout
    ci = subprocess.run(
        command, cwd=root, env={**environment, "CI": "true"},
        capture_output=True, check=True, timeout=10,
    )
    assert ci.stdout.strip() == b"workflow-authority", ci.stdout
PY

echo "hosted-instance auth tests passed"
