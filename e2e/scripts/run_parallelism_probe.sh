#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
E2E_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if ! python3 "$E2E_DIR/../scripts/qa/test_boundary.py"; then
    cat >&2 <<'EOF'
Refusing scheduler parallelism probe launch outside the isolated test runtime.
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

OUT_DIR="$E2E_ARTIFACT_DIR/parallelism-probe"
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
