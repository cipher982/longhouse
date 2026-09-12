#!/usr/bin/env bash
# Run the canonical post-deploy QA against a Longhouse hosted instance.
# This owns hosted continuation-readiness coverage through managed-control
# projection checks; bespoke provider-backed continuation smoke was retired.
#
# Usage:
#   ./scripts/qa-live.sh
#   QA_INSTANCE_SUBDOMAIN=<dedicated-canary> ./scripts/qa-live.sh

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
  echo "Hosted QA requires an explicitly named dedicated canary tenant." >&2
  exit 1
fi

case "${INSTANCE_SUBDOMAIN,,}" in
  david010|demo|dogfood|personal)
    echo "Refusing hosted QA against a personal, dogfood, or demo target." >&2
    exit 1
    ;;
esac

# The runner's shared target helper is authoritative. Reject every alternate
# URL alias unless it names the same configured canary, then clear the aliases
# so ambient frontend/API settings cannot override helper resolution.
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
        echo "Refusing hosted QA with mismatched target URL in ${target_var}." >&2
        exit 1
        ;;
    esac
  fi
done

if [[ -z "${SMOKE_RUNTIME_TOKEN:-}" ]]; then
  echo "Hosted QA requires SMOKE_RUNTIME_TOKEN; production machine-token fallbacks are disabled." >&2
  exit 1
fi

unset LONGHOUSE_DEVICE_TOKEN
unset INSTANCE_URL E2E_INSTANCE_SUBDOMAIN \
  PLAYWRIGHT_BASE_URL PLAYWRIGHT_API_BASE_URL \
  PLAYWRIGHT_FRONTEND_BASE PLAYWRIGHT_BACKEND_URL \
  FRONTEND_URL API_URL E2E_FRONTEND_URL E2E_API_URL
export INSTANCE_SUBDOMAIN

exec "$RUNNER" tests/live/qa-live.spec.ts --timeout=60000 --reporter=line "$@"
