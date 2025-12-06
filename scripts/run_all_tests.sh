#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Master Test Runner – Execute all test suites for Zerg Agent Platform
# ---------------------------------------------------------------------------
# This script orchestrates the complete test suite:
# 1. Backend tests (Python/pytest)
# 2. Frontend tests (Rust/WASM)
# 3. E2E tests (Playwright)
# ---------------------------------------------------------------------------
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FAILED_SUITES=()

echo "🧪 Running complete test suite for Zerg Agent Platform..." >&2
echo "=================================================" >&2

# Function to run a test suite and track failures
run_test_suite() {
    local suite_name="$1"
    local test_command="$2"

    echo "" >&2
    echo "🔄 Running $suite_name tests..." >&2
    echo "---------------------------------" >&2

    if eval "$test_command"; then
        echo "✅ $suite_name tests PASSED" >&2
    else
        echo "❌ $suite_name tests FAILED" >&2
        FAILED_SUITES+=("$suite_name")
    fi
}

# Run Backend Tests
run_test_suite "Backend" "cd '$ROOT_DIR/backend' && ./run_backend_tests.sh"

# Run Frontend Tests
run_test_suite "Frontend" "cd '$ROOT_DIR/frontend' && ./run_frontend_tests.sh"

# Run E2E Tests
run_test_suite "E2E" "cd '$ROOT_DIR/e2e' && ./run_e2e_tests.sh"

# Summary
echo "" >&2
echo "=================================================" >&2
echo "📊 Test Suite Summary:" >&2

if [ ${#FAILED_SUITES[@]} -eq 0 ]; then
    echo "🎉 All test suites PASSED!" >&2
    exit 0
else
    echo "💥 Failed test suites: ${FAILED_SUITES[*]}" >&2
    echo "❌ Overall result: FAILED" >&2
    exit 1
fi
