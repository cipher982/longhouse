#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "${LONGHOUSE_TEST_ISOLATED:-}" != "1" || ! -e /tmp/longhouse-test-isolated ]]; then
    cat >&2 <<'EOF'
Refusing Playwright timeline launch outside the isolated test runtime.
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

OUT_FILE="$E2E_ARTIFACT_DIR/playwright-timeline.json"
rm -f "$OUT_FILE"
mkdir -p "$E2E_ARTIFACT_DIR"

echo "🧪 Running Playwright with timeline reporter"
echo "  Config: playwright.timeline.config.js"
echo "  Output: $OUT_FILE"
echo ""

# Forward any args to Playwright (e.g. tests/foo.spec.ts, --grep, --worker=8, etc.)
set +e
bunx playwright test --config playwright.timeline.config.js "$@"
EXIT_CODE=$?
set -e

echo ""
node scripts/analyze_playwright_timeline.mjs "$OUT_FILE"

exit "$EXIT_CODE"
