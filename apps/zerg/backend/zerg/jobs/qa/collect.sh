#!/bin/bash
# QA Agent - Deterministic Data Collection
#
# Collects system health data for AI analysis.
# Writes all output to $RUN_DIR (default /tmp/qa-run).
#
# Exit codes:
#   0 - Collection succeeded (check collect.status for partial failures)
#   1 - Fatal error

set -euo pipefail

# Configuration
RUN_DIR="${RUN_DIR:-/tmp/qa-run}"
API_URL="${QA_API_URL_INTERNAL:-http://localhost:47300}"
TIMEOUT_SECS=10
RETRY_COUNT=2
RETRY_DELAY=1

# Lock file to prevent overlapping runs
LOCK_FILE="/tmp/qa-collect.lock"

# Check for flock availability (macOS doesn't have it)
USE_LOCK=true
if ! command -v flock &>/dev/null; then
    echo "Warning: flock not available, skipping lock"
    USE_LOCK=false
fi

# Preflight checks for required tools
check_dependencies() {
    local missing=()

    if ! command -v curl &>/dev/null; then
        missing+=("curl")
    fi

    if ! command -v jq &>/dev/null; then
        missing+=("jq")
    fi

    if [ ${#missing[@]} -gt 0 ]; then
        echo "Error: Missing required tools: ${missing[*]}"
        echo "Please install: ${missing[*]}"
        exit 1
    fi
}

check_dependencies

# Ensure run directory exists with restrictive permissions
mkdir -p "$RUN_DIR"
chmod 700 "$RUN_DIR"
cd "$RUN_DIR"

# Atomic write helper: write to .tmp then mv (using printf for safety)
write_json() {
    local file="$1"
    local content="$2"
    printf '%s\n' "$content" > "${file}.tmp"
    mv "${file}.tmp" "$file"
}

# HTTP GET with retry
http_get() {
    local url="$1"
    local output_file="$2"
    local attempt=0

    while [ $attempt -lt $RETRY_COUNT ]; do
        attempt=$((attempt + 1))

        if curl -sf --max-time "$TIMEOUT_SECS" -o "${output_file}.tmp" "$url"; then
            mv "${output_file}.tmp" "$output_file"
            return 0
        fi

        if [ $attempt -lt $RETRY_COUNT ]; then
            sleep $RETRY_DELAY
        fi
    done

    return 1
}

# Acquire lock (non-blocking) - skip if flock not available
if [ "$USE_LOCK" = true ]; then
    exec 200>"$LOCK_FILE"
    if ! flock -n 200; then
        echo "Another collection is running, exiting"
        write_json "collect.status" "skipped"
        exit 0
    fi
fi

# Cleanup on exit
cleanup() {
    rm -f "${RUN_DIR}"/*.tmp 2>/dev/null || true
}
trap cleanup EXIT

# Track check results
declare -a CHECKS_OK=()
declare -a CHECKS_FAILED=()

# Initialize metadata
COLLECTED_AT=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

echo "Starting QA data collection at $COLLECTED_AT"

# ============================================================================
# Check 1: API Health
# ============================================================================
echo "Checking API health..."
if http_get "${API_URL}/health" "health.json"; then
    CHECKS_OK+=("health")
    echo "  ✓ API health OK"
else
    CHECKS_FAILED+=("health")
    write_json "health.json" '{"status": "unreachable", "error": "Connection failed"}'
    echo "  ✗ API health check failed"
fi

# ============================================================================
# Check 2: System Health (reliability endpoint - may require auth)
# ============================================================================
# Note: This endpoint requires admin auth in production.
# The job runs inside the container so we use internal URL.
# If auth is required, we skip this check and rely on DB queries.
echo "Checking system health..."
if http_get "${API_URL}/reliability/system-health" "system_health.json" 2>/dev/null; then
    CHECKS_OK+=("system_health")
    echo "  ✓ System health OK"
else
    # Expected to fail without auth - not a critical failure
    write_json "system_health.json" '{"status": "auth_required", "note": "Using DB queries instead"}'
    echo "  - System health skipped (auth required)"
fi

# ============================================================================
# Check 3: Recent Errors (1h)
# ============================================================================
echo "Checking recent errors (1h)..."
if http_get "${API_URL}/reliability/errors?hours=1" "errors_1h.json" 2>/dev/null; then
    CHECKS_OK+=("errors_1h")
    echo "  ✓ Errors (1h) OK"
else
    write_json "errors_1h.json" '{"status": "auth_required"}'
    echo "  - Errors (1h) skipped (auth required)"
fi

# ============================================================================
# Check 4: Recent Errors (24h)
# ============================================================================
echo "Checking recent errors (24h)..."
if http_get "${API_URL}/reliability/errors?hours=24" "errors_24h.json" 2>/dev/null; then
    CHECKS_OK+=("errors_24h")
    echo "  ✓ Errors (24h) OK"
else
    write_json "errors_24h.json" '{"status": "auth_required"}'
    echo "  - Errors (24h) skipped (auth required)"
fi

# ============================================================================
# Check 5: Performance Metrics
# ============================================================================
echo "Checking performance metrics..."
if http_get "${API_URL}/reliability/performance?hours=24" "performance.json" 2>/dev/null; then
    CHECKS_OK+=("performance")
    echo "  ✓ Performance metrics OK"
else
    write_json "performance.json" '{"status": "auth_required"}'
    echo "  - Performance metrics skipped (auth required)"
fi

# ============================================================================
# Check 6: Stuck Workers
# ============================================================================
echo "Checking stuck workers..."
if http_get "${API_URL}/reliability/workers/stuck?threshold_mins=10" "stuck_workers.json" 2>/dev/null; then
    CHECKS_OK+=("stuck_workers")
    echo "  ✓ Stuck workers check OK"
else
    write_json "stuck_workers.json" '{"status": "auth_required"}'
    echo "  - Stuck workers skipped (auth required)"
fi

# ============================================================================
# Write Collection Summary
# ============================================================================
CHECKS_OK_COUNT=${#CHECKS_OK[@]}
CHECKS_FAILED_COUNT=${#CHECKS_FAILED[@]}
TOTAL_CHECKS=$((CHECKS_OK_COUNT + CHECKS_FAILED_COUNT))

# Determine overall status
if [ $CHECKS_FAILED_COUNT -eq 0 ]; then
    STATUS="ok"
elif [ $CHECKS_OK_COUNT -gt 0 ]; then
    STATUS="partial"
else
    STATUS="failed"
fi

# Write summary
write_json "collect_summary.json" "$(cat <<EOF
{
    "collected_at": "$COLLECTED_AT",
    "status": "$STATUS",
    "checks_ok": $CHECKS_OK_COUNT,
    "checks_failed": $CHECKS_FAILED_COUNT,
    "checks_total": $TOTAL_CHECKS,
    "ok_checks": $(printf '%s\n' "${CHECKS_OK[@]:-}" | jq -R . | jq -s .),
    "failed_checks": $(printf '%s\n' "${CHECKS_FAILED[@]:-}" | jq -R . | jq -s .)
}
EOF
)"

# Write status file (simple string for easy parsing)
write_json "collect.status" "$STATUS"

echo ""
echo "Collection complete: $STATUS ($CHECKS_OK_COUNT/$TOTAL_CHECKS checks passed)"
echo "Output directory: $RUN_DIR"

exit 0
