#!/usr/bin/env bash
# Run live QA against a Longhouse hosted instance.
#
# Usage:
#   ./scripts/qa-live.sh
#   QA_INSTANCE_SUBDOMAIN=other ./scripts/qa-live.sh
#   QA_INSTANCE_URL=https://other.longhouse.ai ./scripts/qa-live.sh

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$ROOT_DIR/scripts/run-prod-e2e.sh"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT_DIR/.env"
  set +a
fi

INSTANCE_SUBDOMAIN="${QA_INSTANCE_SUBDOMAIN:-${INSTANCE_SUBDOMAIN:-david010}}"
INSTANCE_URL="${QA_INSTANCE_URL:-${PLAYWRIGHT_BASE_URL:-}}"

if [[ -n "$INSTANCE_URL" ]]; then
  INSTANCE_URL="${INSTANCE_URL%/}"
  export PLAYWRIGHT_BASE_URL="$INSTANCE_URL"
  export PLAYWRIGHT_API_BASE_URL="${PLAYWRIGHT_API_BASE_URL:-$INSTANCE_URL}"
else
  export INSTANCE_SUBDOMAIN="$INSTANCE_SUBDOMAIN"
fi

exec "$RUNNER" tests/live/qa-live.spec.ts --timeout=60000 --reporter=line "$@"
