#!/usr/bin/env bash
# Run one hosted QA target against the QA instance inside the credential-free isolation image.
#
#   run-hosted-target.sh TARGET "KEY1 KEY2 ..." [extra --set KEY=VALUE ...]
#
# Needs QA_INSTANCE_SUBDOMAIN and QA_IMAGE in the environment. The named environment values go to the
# container as a 0600 credentials file (never as argv or env), removed on exit; every other setting is
# derived from the QA instance. Called by .github/workflows/release.yml (the qa job).
set -euo pipefail

target="${1:?target}"
keys="${2:?space-separated credential names}"
shift 2
: "${QA_INSTANCE_SUBDOMAIN:?}" "${QA_IMAGE:?}"
base="https://${QA_INSTANCE_SUBDOMAIN}.longhouse.ai"

credentials="$(mktemp "${RUNNER_TEMP:-/tmp}/hosted-credentials.XXXXXX.json")"
trap 'rm -f "$credentials"' EXIT INT TERM
chmod 600 "$credentials"
KEYS="$keys" python3 - "$credentials" <<'PY'
import json
import os
import sys

keys = os.environ["KEYS"].split()
with open(sys.argv[1], "w", encoding="utf-8") as stream:
    json.dump({key: os.environ[key] for key in keys if os.environ.get(key)}, stream)
PY

settings=(
  --set "QA_INSTANCE_SUBDOMAIN=$QA_INSTANCE_SUBDOMAIN"
  --set "LONGHOUSE_API_URL=$base"
  --set "PLAYWRIGHT_BASE_URL=$base"
  --set "PLAYWRIGHT_API_BASE_URL=$base"
)
[[ -z "${CONTROL_PLANE_URL:-}" ]] || settings+=(--set "CONTROL_PLANE_URL=$CONTROL_PLANE_URL")

python3 scripts/qa/test-isolation.py --live --image "$QA_IMAGE" --credentials "$credentials" \
  --target "$target" "${settings[@]}" "$@"
