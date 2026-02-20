#!/usr/bin/env bash
# Run live QA against a Longhouse instance (default: david010.longhouse.ai)
#
# Usage:
#   ./scripts/qa-live.sh
#   ./scripts/qa-live.sh --url https://other.longhouse.ai
#   QA_INSTANCE_URL=https://other.longhouse.ai ./scripts/qa-live.sh
#
# Environment:
#   LONGHOUSE_PASSWORD  - Instance password (auto-fetched from container if not set)
#   QA_INSTANCE_URL     - Override instance URL (default: https://david010.longhouse.ai)
#   QA_CONTAINER        - Docker container name for password lookup (default: longhouse-david010)

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# Load repo .env if present (local only; no auto-creation)
if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$ROOT_DIR/.env"
  set +a
fi

INSTANCE_URL="${QA_INSTANCE_URL:-https://david010.longhouse.ai}"
CONTAINER="${QA_CONTAINER:-longhouse-david010}"

# Parse CLI args
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
    --container)
      CONTAINER="$2"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--url https://instance.longhouse.ai] [--container <name>]"
      echo ""
      echo "Environment variables:"
      echo "  LONGHOUSE_PASSWORD  Password for the instance (auto-fetched if not set)"
      echo "  QA_INSTANCE_URL     Instance URL override"
      echo "  QA_CONTAINER        Docker container name for password lookup"
      exit 0
      ;;
    *)
      # Pass through any remaining args to Playwright
      break
      ;;
  esac
done

# Strip trailing slash
INSTANCE_URL="${INSTANCE_URL%/}"

echo ""
echo "================================================"
echo "  Longhouse Live QA"
echo "  Instance: $INSTANCE_URL"
echo "================================================"
echo ""

# Get password from running container (or from env if already set)
if [[ -z "${LONGHOUSE_PASSWORD:-}" ]]; then
  echo "Fetching LONGHOUSE_PASSWORD from container ${CONTAINER}..."
  LONGHOUSE_PASSWORD="$(ssh zerg "docker exec ${CONTAINER} env | grep '^LONGHOUSE_PASSWORD=' | cut -d= -f2-" 2>/dev/null || true)"

  # Strip surrounding quotes if any
  LONGHOUSE_PASSWORD="${LONGHOUSE_PASSWORD#\'}"
  LONGHOUSE_PASSWORD="${LONGHOUSE_PASSWORD%\'}"
  LONGHOUSE_PASSWORD="${LONGHOUSE_PASSWORD#\"}"
  LONGHOUSE_PASSWORD="${LONGHOUSE_PASSWORD%\"}"
  # Strip trailing newline/carriage-return only — not all whitespace (passwords can contain spaces)
  LONGHOUSE_PASSWORD="$(echo "$LONGHOUSE_PASSWORD" | tr -d '\n\r')"
fi

if [[ -z "${LONGHOUSE_PASSWORD:-}" ]]; then
  echo "ERROR: Could not get LONGHOUSE_PASSWORD." >&2
  echo "  Set LONGHOUSE_PASSWORD env var, or ensure 'ssh zerg docker exec ${CONTAINER} env' works." >&2
  exit 1
fi

echo "Password obtained (${#LONGHOUSE_PASSWORD} chars)."
echo ""

export LONGHOUSE_PASSWORD
export QA_BASE_URL="$INSTANCE_URL"
export RUN_LIVE_E2E="1"

# Run Playwright against the live spec
cd "$ROOT_DIR/apps/zerg/e2e"

bunx playwright test tests/live/qa-live.spec.ts \
  --config playwright.prod.config.js \
  --timeout=60000 \
  --reporter=line \
  "$@"
