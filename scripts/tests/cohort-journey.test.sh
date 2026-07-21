#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RUNNER="$ROOT_DIR/scripts/qa/cohort-journey.sh"

if env -u SMOKE_RUNTIME_TOKEN -u LONGHOUSE_DEVICE_TOKEN "$RUNNER" >/dev/null 2>&1; then
  echo "cohort journey unexpectedly accepted missing authentication" >&2
  exit 1
fi

if SMOKE_RUNTIME_TOKEN="test-only" QA_INSTANCE_SUBDOMAIN="demo" "$RUNNER" >/dev/null 2>&1; then
  echo "cohort journey unexpectedly accepted the demo tenant" >&2
  exit 1
fi

echo "cohort journey fail-closed checks passed"
