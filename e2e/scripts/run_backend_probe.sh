#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$E2E_DIR"

OUT_FILE="test-results/backend-probe-timeline.json"
rm -f "$OUT_FILE"
mkdir -p "test-results"

export PROBE_TEST_COUNT="${PROBE_TEST_COUNT:-64}"
export PROBE_HOLD_MS="${PROBE_HOLD_MS:-250}"

echo "🧪 Running backend parallelism probe"
echo "  Config: playwright.backend-probe.config.js"
echo "  Output: $OUT_FILE"
echo "  Tests:  PROBE_TEST_COUNT=$PROBE_TEST_COUNT"
echo "  Hold:   PROBE_HOLD_MS=$PROBE_HOLD_MS"
echo ""

set +e
bunx playwright test --config playwright.backend-probe.config.js
EXIT_CODE=$?
set -e

echo ""
node scripts/analyze_playwright_timeline.mjs "$OUT_FILE"

exit "$EXIT_CODE"
