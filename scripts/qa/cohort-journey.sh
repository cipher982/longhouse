#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER="$ROOT_DIR/scripts/qa/run-prod-e2e.sh"

if [[ -z "${SMOKE_RUNTIME_TOKEN:-}" && -z "${LONGHOUSE_DEVICE_TOKEN:-}" ]]; then
  echo "Cohort journey requires SMOKE_RUNTIME_TOKEN or LONGHOUSE_DEVICE_TOKEN." >&2
  exit 2
fi

INSTANCE_SUBDOMAIN="${QA_INSTANCE_SUBDOMAIN:-${INSTANCE_SUBDOMAIN:-${E2E_INSTANCE_SUBDOMAIN:-}}}"
if [[ "$INSTANCE_SUBDOMAIN" == "demo" ]]; then
  echo "Cohort journey refuses the demo tenant; configure the non-demo dogfood subdomain." >&2
  exit 2
fi

OUTPUT="${LONGHOUSE_JOURNEY_OUTPUT:-$ROOT_DIR/artifacts/cohort-journey/cohort-journey.json}"
RAW_OUTPUT="$(mktemp -d "${TMPDIR:-/tmp}/longhouse-cohort-journey.XXXXXX")"
cleanup() {
  rm -rf "$RAW_OUTPUT"
}
trap cleanup EXIT INT TERM

mkdir -p "$(dirname "$OUTPUT")"
export LONGHOUSE_JOURNEY_OUTPUT="$OUTPUT"
export LONGHOUSE_JOURNEY_PRIVACY_MODE=1
export LONGHOUSE_JOURNEY_RAW_OUTPUT_DIR="$RAW_OUTPUT"
export INSTANCE_SUBDOMAIN

set +e
"$RUNNER" tests/live/cohort-journey.spec.ts
status=$?
set -e

if [[ ! -s "$OUTPUT" ]]; then
  echo "Cohort journey did not produce its privacy-safe artifact." >&2
  exit 1
fi
exit "$status"
