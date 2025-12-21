#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$E2E_DIR"

OUT_DIR="test-results/parallelism-probe"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

export PROBE_TEST_COUNT="${PROBE_TEST_COUNT:-64}"
export PROBE_SLEEP_MS="${PROBE_SLEEP_MS:-2000}"

echo "🧪 Running scheduler parallelism probe"
echo "  Config: playwright.probe.config.js"
echo "  Tests:  PROBE_TEST_COUNT=$PROBE_TEST_COUNT"
echo "  Sleep:  PROBE_SLEEP_MS=$PROBE_SLEEP_MS"
echo ""

bunx playwright test --config playwright.probe.config.js probes/scheduler_parallelism.probe.spec.ts

echo ""
node scripts/analyze_parallelism_probe.mjs
