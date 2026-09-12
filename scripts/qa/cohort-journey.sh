#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER="$ROOT_DIR/scripts/qa/run-prod-e2e.sh"

# Local convenience only; CI authority comes exclusively from workflow env.
if [[ -z "${CI:-}" && -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT_DIR/.env"
  set +a
fi

INSTANCE_SUBDOMAIN="${QA_INSTANCE_SUBDOMAIN:-}"
if [[ -z "$INSTANCE_SUBDOMAIN" ]]; then
  echo "Cohort journey requires an explicitly named dedicated canary tenant." >&2
  exit 2
fi

case "${INSTANCE_SUBDOMAIN,,}" in
  david010|demo|dogfood|personal)
    echo "Refusing cohort journey against a personal, dogfood, or demo target." >&2
    exit 2
    ;;
esac

# Keep the shared runner/helper as the target authority. Do not let an
# inherited frontend or API alias bypass the explicitly selected canary.
CANARY_ORIGIN="https://${INSTANCE_SUBDOMAIN}.longhouse.ai"
for target_var in \
  QA_INSTANCE_URL INSTANCE_URL \
  PLAYWRIGHT_BASE_URL PLAYWRIGHT_API_BASE_URL \
  PLAYWRIGHT_FRONTEND_BASE PLAYWRIGHT_BACKEND_URL \
  FRONTEND_URL API_URL E2E_FRONTEND_URL E2E_API_URL; do
  target_value="${!target_var:-}"
  if [[ -n "$target_value" ]]; then
    case "$target_value" in
      "$CANARY_ORIGIN"|"$CANARY_ORIGIN/"*) ;;
      *)
        echo "Refusing cohort journey with mismatched target URL in ${target_var}." >&2
        exit 2
        ;;
    esac
  fi
done

if [[ -z "${SMOKE_RUNTIME_TOKEN:-}" ]]; then
  echo "Cohort journey requires SMOKE_RUNTIME_TOKEN; ambient machine-token fallbacks are disabled." >&2
  exit 2
fi

unset LONGHOUSE_DEVICE_TOKEN

OUTPUT="${LONGHOUSE_JOURNEY_OUTPUT:-$ROOT_DIR/artifacts/cohort-journey/cohort-journey.json}"
if [[ "$OUTPUT" != /* ]]; then
  OUTPUT="$ROOT_DIR/$OUTPUT"
fi
RAW_OUTPUT="$(mktemp -d "${TMPDIR:-/tmp}/longhouse-cohort-journey.XXXXXX")"
cleanup() {
  rm -rf "$RAW_OUTPUT"
}
trap cleanup EXIT INT TERM

mkdir -p "$(dirname "$OUTPUT")"
export LONGHOUSE_JOURNEY_OUTPUT="$OUTPUT"
export LONGHOUSE_JOURNEY_PRIVACY_MODE=1
export LONGHOUSE_JOURNEY_RAW_OUTPUT_DIR="$RAW_OUTPUT"
unset INSTANCE_URL E2E_INSTANCE_SUBDOMAIN \
  PLAYWRIGHT_BASE_URL PLAYWRIGHT_API_BASE_URL \
  PLAYWRIGHT_FRONTEND_BASE PLAYWRIGHT_BACKEND_URL \
  FRONTEND_URL API_URL E2E_FRONTEND_URL E2E_API_URL
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
