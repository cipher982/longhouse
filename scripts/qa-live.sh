#!/usr/bin/env bash
# Run live QA against a Longhouse hosted instance (default subdomain: david010)
#
# Usage:
#   ./scripts/qa-live.sh
#   ./scripts/qa-live.sh --subdomain other
#   ./scripts/qa-live.sh --url https://other.longhouse.ai
#   QA_INSTANCE_SUBDOMAIN=other ./scripts/qa-live.sh
#
# Environment:
#   QA_INSTANCE_URL         - Direct instance URL override
#   QA_INSTANCE_SUBDOMAIN   - Hosted instance subdomain (default: david010)
#   CONTROL_PLANE_ADMIN_TOKEN / SMOKE_LOGIN_TOKEN - Hosted auth inputs

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUNNER="$ROOT_DIR/scripts/run-prod-e2e.sh"

# Load repo .env if present (local only; no auto-creation)
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT_DIR/.env"
  set +a
fi

INSTANCE_SUBDOMAIN="${QA_INSTANCE_SUBDOMAIN:-david010}"
INSTANCE_URL="${QA_INSTANCE_URL:-}"

while [[ $# -gt 0 ]]; do
  case $1 in
    --url)
      INSTANCE_URL="$2"
      shift 2
      ;;
    --url=*)
      INSTANCE_URL="${1#*=}"
      shift
      ;;
    --subdomain)
      INSTANCE_SUBDOMAIN="$2"
      shift 2
      ;;
    --subdomain=*)
      INSTANCE_SUBDOMAIN="${1#*=}"
      shift
      ;;
    -h|--help)
      echo "Usage: $0 [--subdomain name] [--url https://instance.longhouse.ai]"
      echo ""
      echo "Environment variables:"
      echo "  QA_INSTANCE_URL         Direct instance URL override"
      echo "  QA_INSTANCE_SUBDOMAIN   Hosted instance subdomain (default: david010)"
      echo "  CONTROL_PLANE_ADMIN_TOKEN or SMOKE_LOGIN_TOKEN required for hosted auth"
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

if [[ -n "$INSTANCE_URL" ]]; then
  INSTANCE_URL="${INSTANCE_URL%/}"
  export PLAYWRIGHT_BASE_URL="$INSTANCE_URL"
  export PLAYWRIGHT_API_BASE_URL="$INSTANCE_URL"
else
  export INSTANCE_SUBDOMAIN="$INSTANCE_SUBDOMAIN"
fi

echo ""
echo "================================================"
echo "  Longhouse Live QA"
if [[ -n "$INSTANCE_URL" ]]; then
  echo "  Instance URL: $INSTANCE_URL"
else
  echo "  Instance subdomain: $INSTANCE_SUBDOMAIN"
fi
echo "================================================"
echo ""

exec "$RUNNER" tests/live/qa-live.spec.ts \
  --timeout=60000 \
  --reporter=line \
  "$@"
