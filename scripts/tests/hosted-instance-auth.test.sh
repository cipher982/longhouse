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

# The deployment transport test isolates control-plane authentication from the
# registry inspector; exact-image metadata is covered by release-artifacts.test.py.
_lh_hosted_resolve_image_metadata() {
  export LH_DEPLOYMENT_SOURCE_SHA="0123456789abcdef0123456789abcdef01234567"
  export LH_DEPLOYMENT_SCHEMA_VERSION="5"
  export LH_DEPLOYMENT_SCHEMA_MIN_READER="5"
  export LH_DEPLOYMENT_SCHEMA_MAX_READER="5"
}

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

DEPLOYMENT_SCENARIO="success"

curl() {
  local data=""
  local output_file=""
  local request_url=""
  local deployment_id=""
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -d) data="$2"; shift 2 ;;
      -o) output_file="$2"; shift 2 ;;
      -w|-H|-X|--connect-timeout|--max-time) shift 2 ;;
      *) request_url="$1"; shift ;;
    esac
  done

  case "$request_url" in
    https://control.longhouse.ai/api/deployments)
      if [[ "$data" != *'"target_instance_ids":[7]'* || "$data" != *'"ready":true'* ]]; then
        echo "Expected durable submission to persist explicit target membership and readiness" >&2
        return 1
      fi
      if [[ "$data" != *'"schema_version":"5"'* || "$data" != *'"schema_min_reader":"5"'* || "$data" != *'"schema_max_reader":"5"'* ]]; then
        echo "Expected exact candidate schema metadata in durable submission" >&2
        return 1
      fi
      case "$DEPLOYMENT_SCENARIO" in
        success) deployment_id="d-test-1" ;;
        wrong-receipt) deployment_id="d-wrong-receipt" ;;
        wrong-digest) deployment_id="d-wrong-digest" ;;
        wrong-target) deployment_id="d-wrong-target" ;;
        failed-target) deployment_id="d-failed-target" ;;
        *) echo "Unknown deployment scenario: $DEPLOYMENT_SCENARIO" >&2; return 1 ;;
      esac
      printf '{"id":"%s","status":"queued","image":"%s","image_digest":"%s"}' \
        "$deployment_id" "$image" "$image" >"$output_file"
      printf '201'
      ;;
    https://control.longhouse.ai/api/deployments/*)
      deployment_id="${request_url##*/}"
      case "$DEPLOYMENT_SCENARIO" in
        success)
          printf '{"id":"%s","status":"success","image":"%s","image_digest":"%s","targets":[{"id":7,"deploy_state":"success"}]}' \
            "$deployment_id" "$image" "$image" >"$output_file"
          ;;
        wrong-receipt)
          printf '{"id":"d-not-the-requested-receipt","status":"success","image":"%s","image_digest":"%s","targets":[{"id":7,"deploy_state":"success"}]}' \
            "$image" "$image" >"$output_file"
          ;;
        wrong-digest)
          printf '{"id":"%s","status":"success","image":"%s","image_digest":"ghcr.io/cipher982/longhouse-runtime@sha256:%064d","targets":[{"id":7,"deploy_state":"success"}]}' \
            "$deployment_id" "$image" 2 >"$output_file"
          ;;
        wrong-target)
          printf '{"id":"%s","status":"success","image":"%s","image_digest":"%s","targets":[{"id":8,"deploy_state":"success"}]}' \
            "$deployment_id" "$image" "$image" >"$output_file"
          ;;
        failed-target)
          printf '{"id":"%s","status":"success","image":"%s","image_digest":"%s","targets":[{"id":7,"deploy_state":"failure"}]}' \
            "$deployment_id" "$image" "$image" >"$output_file"
          ;;
      esac
      printf '200'
      ;;
    *)
      echo "Unexpected deployment API URL: $request_url" >&2
      return 1
      ;;
  esac
}

export CONTROL_PLANE_URL="https://control.longhouse.ai"
export CONTROL_PLANE_ADMIN_TOKEN="admin-token-from-env"
export INSTANCE_SUBDOMAIN="demo"
export LH_HOSTED_DEPLOYMENT_POLL_SECONDS=0
export LH_DEPLOYMENT_IDEMPOTENCY_KEY="deployment-test-1"
export LH_DEPLOYMENT_SCHEMA_VERSION="5"
export LH_DEPLOYMENT_SCHEMA_MIN_READER="5"
export LH_DEPLOYMENT_SCHEMA_MAX_READER="5"
image="ghcr.io/cipher982/longhouse-runtime@sha256:$(printf '1%.0s' {1..64})"
lh_hosted_reprovision "7" "$image"

if [[ "$LH_DEPLOYMENT_ID" != "d-test-1" ||
      "$LH_DEPLOYMENT_STATUS" != "success" ||
      "$LH_DEPLOYMENT_IMAGE" != "$image" ||
      "$LH_DEPLOYMENT_IMAGE_DIGEST" != "$image" ||
      "$LH_DEPLOYMENT_TARGET_ID" != "7" ||
      "$LH_DEPLOYMENT_TARGET_STATE" != "success" ]]; then
  echo "Expected durable deployment receipt to prove exact image and target success"
  exit 1
fi

expect_reprovision_failure() {
  local scenario="$1"
  DEPLOYMENT_SCENARIO="$scenario"
  if lh_hosted_reprovision "7" "$image" >/dev/null 2>&1; then
    echo "Expected ${scenario} deployment receipt to be rejected"
    exit 1
  fi
}

expect_reprovision_failure wrong-receipt
expect_reprovision_failure wrong-digest
expect_reprovision_failure wrong-target
expect_reprovision_failure failed-target

DEPLOYMENT_SCENARIO="success"
if lh_hosted_reprovision "7" "ghcr.io/cipher982/longhouse-runtime:mutable-tag" >/dev/null 2>&1; then
  echo "Expected mutable deployment image to be rejected before API submission"
  exit 1
fi

unset LH_DEPLOYMENT_IDEMPOTENCY_KEY LH_HOSTED_DEPLOYMENT_POLL_SECONDS
unset LH_DEPLOYMENT_SCHEMA_VERSION LH_DEPLOYMENT_SCHEMA_MIN_READER LH_DEPLOYMENT_SCHEMA_MAX_READER

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
