#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# Master Test Runner – Execute all test suites for Zerg Agent Platform
# ---------------------------------------------------------------------------
# This script orchestrates the complete test suite:
# 1. Jarvis tests (bun)
# 2. Zerg tests (backend + frontend + e2e)
#
# Prefer the Make targets directly:
#   make test
#   make test-jarvis
#   make test-zerg
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

run_test_suite "Jarvis" "cd '$ROOT_DIR' && make test-jarvis"
run_test_suite "Zerg" "cd '$ROOT_DIR' && make test-zerg"

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
