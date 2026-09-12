#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if ! python3 "$E2E_DIR/../scripts/qa/test_boundary.py"; then
    cat >&2 <<'EOF'
Refusing backend parallelism probe launch outside the isolated test runtime.
Use `make test-e2e` or the portable `scripts/qa/test-isolation.py --command` wrapper.
EOF
    exit 2
fi
if [[ -z "${E2E_ARTIFACT_DIR:-}" ]]; then
    echo "E2E_ARTIFACT_DIR must be set by the isolated test runtime" >&2
    exit 2
fi
case "$E2E_ARTIFACT_DIR" in
    /*) ;;
    *)
        echo "E2E_ARTIFACT_DIR must be an absolute isolated artifact path" >&2
        exit 2
        ;;
esac

cd "$E2E_DIR"

OUT_FILE="$E2E_ARTIFACT_DIR/backend-probe/backend-probe-timeline.json"
rm -f "$OUT_FILE"
mkdir -p "$(dirname "$OUT_FILE")"

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
