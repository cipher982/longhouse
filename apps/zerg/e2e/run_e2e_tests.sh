#!/bin/bash
# ---------------------------------------------------------------------------
# UNIFIED E2E TEST RUNNER
# ---------------------------------------------------------------------------
# Smart, consolidated E2E testing system that replaces multiple test runners
# with a single, mode-aware test execution system.
#
# Usage:
#   ./run_unified_e2e.sh                    # Full comprehensive suite
#   ./run_unified_e2e.sh --mode=quick       # Quick validation tests
#   ./run_unified_e2e.sh --mode=debug       # Debug and diagnostic tests
#   ./run_unified_e2e.sh --mode=core        # Core functionality only
#   ./run_unified_e2e.sh --mode=advanced    # Advanced tests (perf, a11y, etc.)
# ---------------------------------------------------------------------------

set -euo pipefail

# Default configuration
MODE="full"
VERBOSE=false
PARALLEL=true
WORKERS_OVERRIDE=""
REPORTS_DIR="test-reports"
TIMESTAMP=$(date +"%Y%m%d_%H%M%S")

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# Function to print colored output
print_color() {
    local color=$1
    local message=$2
    echo -e "${color}${message}${NC}"
}

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --mode=*)
            MODE="${1#*=}"
            shift
            ;;
        --commis=*)
            WORKERS_OVERRIDE="${1#*=}"
            shift
            ;;
        --verbose|-v)
            VERBOSE=true
            shift
            ;;
        --no-parallel)
            PARALLEL=false
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --mode=MODE           Test mode: full, basic"
            echo "  --commis=N           Override Playwright commis (default: use playwright.config.js)"
            echo "  --verbose, -v         Verbose output"
            echo "  --no-parallel         Disable parallel execution"
            echo "  --help, -h           Show this help"
            echo ""
            echo "Modes:"
            echo "  full                Full comprehensive test suite (default)"
            echo "  basic               Essential tests only (~3 min)"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Ensure we run from the e2e directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

print_color $BLUE "🚀 Unified E2E Test Runner"
print_color $BLUE "=========================="
print_color $CYAN "Mode: $MODE"
print_color $CYAN "Timestamp: $TIMESTAMP"
echo ""

# Create reports directory
mkdir -p "$REPORTS_DIR"

# Test categories organized by mode
get_test_files() {
    case $1 in
        basic)
            echo "fiche_creation_full.spec.ts comprehensive_debug.spec.ts canvas_complete_workflow.spec.ts canvas_api_contract.spec.ts"
            ;;
        full)
            echo "fiche_creation_full.spec.ts comprehensive_debug.spec.ts canvas_complete_workflow.spec.ts canvas_api_contract.spec.ts workflow_execution_http.spec.ts tool_palette_node_connections.spec.ts realtime_websocket_monitoring.spec.ts error_handling_edge_cases.spec.ts data_persistence_recovery.spec.ts performance_load_testing.spec.ts accessibility_ui_ux.spec.ts multi_user_concurrency.spec.ts"
            ;;
        *)
            echo ""
            ;;
    esac
}

# Validate mode
if [[ -z "$(get_test_files $MODE)" ]]; then
    print_color $RED "❌ Invalid mode: $MODE"
    print_color $YELLOW "Valid modes: basic, full"
    exit 1
fi

# Pre-flight checks
print_color $YELLOW "🔧 Pre-flight checks..."

# Check if Playwright is installed (bunx or npx)
if ! command -v bunx &> /dev/null && ! command -v npx &> /dev/null; then
    print_color $RED "❌ Neither bunx nor npx found. Please install Bun or Node.js."
    exit 1
fi

# Check if Playwright config exists
if [ ! -f "playwright.config.js" ] && [ ! -f "playwright.config.ts" ]; then
    print_color $RED "❌ Playwright config not found (looking for playwright.config.js or playwright.config.ts)"
    exit 1
fi

# Check if jq is available for JSON parsing
if ! command -v jq &> /dev/null; then
    print_color $YELLOW "⚠️  jq not found - test summary will be basic (install jq for detailed breakdown)"
    HAS_JQ=false
else
    HAS_JQ=true
fi

print_color $GREEN "✅ Pre-flight checks completed"
print_color $CYAN "ℹ️  Playwright will automatically start backend and frontend servers"
echo ""

# Setup test environment
print_color $YELLOW "🛠️  Setting up test environment..."

# Create cache and temp directories
export PLAYWRIGHT_CACHE_DIR="${SCRIPT_DIR}/.pw_cache"
mkdir -p "$PLAYWRIGHT_CACHE_DIR"
export TMPDIR="${SCRIPT_DIR}/.pw_tmp"
mkdir -p "$TMPDIR"
export PW_TMP_DIR="$TMPDIR"

# Set environment variables
export NODE_ENV=test
export E2E_LOG_SUPPRESS=1

print_color $GREEN "✅ Environment setup completed"
echo ""

# Get test files for the selected mode
TEST_FILES=$(get_test_files $MODE)

print_color $YELLOW "🧪 Running $MODE tests..."
print_color $CYAN "Test files: $TEST_FILES"
echo ""

# Build Playwright command based on mode and options
PLAYWRIGHT_CMD="bunx playwright test"

# Add test file filters
if [[ -n "$TEST_FILES" ]]; then
    for file in $TEST_FILES; do
        PLAYWRIGHT_CMD="$PLAYWRIGHT_CMD tests/$file"
    done
fi

# Add reporter based on mode (JSON will be added separately for summary)
case $MODE in
    basic)
        PLAYWRIGHT_CMD="$PLAYWRIGHT_CMD --reporter=line"
        ;;
    full)
        PLAYWRIGHT_CMD="$PLAYWRIGHT_CMD --reporter=list"
        ;;
esac

# Add parallel execution unless disabled
if [[ "$PARALLEL" == "true" ]]; then
    if [[ -n "$WORKERS_OVERRIDE" ]]; then
        PLAYWRIGHT_CMD="$PLAYWRIGHT_CMD --commis=$WORKERS_OVERRIDE"
    fi
else
    # Explicitly force serial mode (useful for debugging). Otherwise defer to playwright.config.js.
    PLAYWRIGHT_CMD="$PLAYWRIGHT_CMD --commis=1"
fi

# Add verbose output if requested
if [[ "$VERBOSE" == "true" ]]; then
    PLAYWRIGHT_CMD="$PLAYWRIGHT_CMD --verbose"
fi

# Add timeout based on mode
case $MODE in
    basic)
        PLAYWRIGHT_CMD="$PLAYWRIGHT_CMD --timeout=30000"
        ;;
    full)
        PLAYWRIGHT_CMD="$PLAYWRIGHT_CMD --timeout=60000"
        ;;
esac

# Execute the tests
print_color $BLUE "🚀 Executing: $PLAYWRIGHT_CMD"
echo ""

START_TIME=$(date +%s)

# Run the tests and capture exit code
set +e
TEMP_OUTPUT="${REPORTS_DIR}/temp_output_${TIMESTAMP}.txt"
eval "$PLAYWRIGHT_CMD" 2>&1 | tee "$TEMP_OUTPUT"
TEST_EXIT_CODE=$?
set -e

END_TIME=$(date +%s)
DURATION=$((END_TIME - START_TIME))

echo ""
print_color $BLUE "📊 Test Execution Summary"
print_color $BLUE "========================"
echo "Mode: $MODE"
echo "Duration: ${DURATION}s"
echo "Exit Code: $TEST_EXIT_CODE"

# Parse test results from output for pytest-style summary
if [[ -f "$TEMP_OUTPUT" ]]; then
    echo ""

    # Extract basic stats from Playwright output (works with different reporters)
    PASSED=$(grep -o "[0-9]\+ passed" "$TEMP_OUTPUT" | grep -o "[0-9]\+" | head -1 || echo 0)
    FAILED=$(grep -o "[0-9]\+ failed" "$TEMP_OUTPUT" | grep -o "[0-9]\+" | head -1 || echo 0)
    SKIPPED=$(grep -o "[0-9]\+ skipped" "$TEMP_OUTPUT" | grep -o "[0-9]\+" | head -1 || echo 0)

    # If we can't parse the numbers, try to get them from the exit code and output patterns
    if [[ "$PASSED" == "0" && "$FAILED" == "0" && "$SKIPPED" == "0" ]]; then
        if [[ $TEST_EXIT_CODE -eq 0 ]]; then
            # Count test files or estimate from test descriptions
            PASSED=$(grep -c "✓" "$TEMP_OUTPUT" 2>/dev/null || echo "unknown")
            FAILED=0
        else
            # Look for error patterns
            FAILED=$(grep -c "✗\|×\|FAIL" "$TEMP_OUTPUT" 2>/dev/null || echo "1+")
            PASSED=$(grep -c "✓\|PASS" "$TEMP_OUTPUT" 2>/dev/null || echo "0")
        fi
    fi

    # Summary line like pytest
    if [[ $TEST_EXIT_CODE -eq 0 ]]; then
        STATUS_COLOR=$GREEN
        STATUS_ICON="✅"
    else
        STATUS_COLOR=$RED
        STATUS_ICON="❌"
    fi

    printf "${STATUS_COLOR}${STATUS_ICON} "
    if [[ "$PASSED" != "0" ]]; then
        printf "${GREEN}$PASSED passed${NC}"
    fi
    if [[ "$FAILED" != "0" && "$FAILED" != "" ]]; then
        if [[ "$PASSED" != "0" ]]; then printf ", "; fi
        printf "${RED}$FAILED failed${NC}"
    fi
    if [[ "$SKIPPED" != "0" && "$SKIPPED" != "" ]]; then
        if [[ "$PASSED" != "0" || "$FAILED" != "0" ]]; then printf ", "; fi
        printf "${YELLOW}$SKIPPED skipped${NC}"
    fi
    echo " in ${DURATION}s"

    # Show failing test names if we can extract them
    if [[ $TEST_EXIT_CODE -ne 0 ]]; then
        echo ""
        print_color $RED "💥 FAILURES:"
        # Try to extract error details from common Playwright output patterns
        grep -A 2 -B 1 "✗\|×\|FAIL\|Error:" "$TEMP_OUTPUT" 2>/dev/null | head -10 | sed 's/^/  /' || \
        grep -A 1 "failing" "$TEMP_OUTPUT" 2>/dev/null | head -5 | sed 's/^/  /' || \
        echo "  Check the full output above for details"
    fi
else
    echo ""
    if [[ $TEST_EXIT_CODE -eq 0 ]]; then
        print_color $GREEN "✅ Tests completed successfully"
    else
        print_color $RED "❌ Some tests failed"
    fi
fi

# Generate mode-specific summary
case $MODE in
    basic)
        if [[ $TEST_EXIT_CODE -eq 0 ]]; then
            print_color $GREEN "✅ Basic tests passed - core functionality working"
        else
            print_color $RED "❌ Basic tests failed - check core functionality"
        fi
        ;;
    full)
        if [[ $TEST_EXIT_CODE -eq 0 ]]; then
            print_color $GREEN "🎉 Full test suite passed - this is indeed the best tested app!"
        else
            print_color $RED "❌ Full test suite failed - check detailed reports"
        fi
        ;;
esac

# Clean up test databases
print_color $YELLOW "🧹 Cleaning up test databases..."
if [ -f "../backend/cleanup_test_dbs.sh" ]; then
    (cd ../backend && ./cleanup_test_dbs.sh)
fi

# Clean up temp files for basic mode
if [[ "$MODE" == "basic" ]]; then
    rm -rf "$TMPDIR" 2>/dev/null || true
fi

print_color $GREEN "✅ Cleanup completed"
echo ""

# Final status
if [[ $TEST_EXIT_CODE -eq 0 ]]; then
    print_color $GREEN "🎯 E2E Test Suite Completed Successfully"
else
    print_color $RED "💥 E2E Test Suite Failed"
fi

exit $TEST_EXIT_CODE
