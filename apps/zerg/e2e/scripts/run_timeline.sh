#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$E2E_DIR"

OUT_FILE="test-results/playwright-timeline.json"
rm -f "$OUT_FILE"
mkdir -p "test-results"

echo "🧪 Running Playwright with timeline reporter"
echo "  Config: playwright.timeline.config.js"
echo "  Output: $OUT_FILE"
echo ""

# Forward any args to Playwright (e.g. tests/foo.spec.ts, --grep, --workers=8, etc.)
set +e
bunx playwright test --config playwright.timeline.config.js "$@"
EXIT_CODE=$?
set -e

echo ""
node scripts/analyze_playwright_timeline.mjs "$OUT_FILE"

exit "$EXIT_CODE"
