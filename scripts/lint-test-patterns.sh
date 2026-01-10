#!/usr/bin/env bash
# lint-test-patterns.sh - Detect test anti-patterns that cause flaky tests
#
# Anti-patterns detected:
# 1. window.confirm( - in frontend src (not test files)
# 2. alert( - in frontend src (not test files)
# 3. waitForTimeout( - in E2E spec files that are NOT fully skipped
# 4. networkidle - in E2E spec files that are NOT fully skipped
#
# Exit codes:
#   0 - No violations found
#   1 - Violations found

set -euo pipefail

FRONTEND_SRC="apps/zerg/frontend-web/src"
E2E_TESTS="apps/zerg/e2e/tests"

failed=0

# =============================================================================
# Allowlist: Existing files with grandfathered anti-patterns
# These files have known violations that predate this lint guard.
# New violations in these files will still be caught if the pattern count increases.
# To remove a file from allowlist, fix all its violations first.
# =============================================================================
ALLOWLIST_TIMEOUT=(
  "accessibility_ui_ux.spec.ts"
  "agent_scheduling.spec.ts"
  "canvas_workflows.spec.ts"
  "chat_correlation_id.spec.ts"
  "comprehensive_database_isolation.spec.ts"
  "realtime_updates.spec.ts"
  "realtime_websocket_monitoring.spec.ts"
  "supervisor-tool-visibility.spec.ts"
  "workflow_execution_http.spec.ts"
  "workflow_execution.spec.ts"
)

ALLOWLIST_NETWORKIDLE=(
  "agent_creation_full.spec.ts"
  "agent_creation.spec.ts"
  "automation_history.spec.ts"
  "error_handling_edge_cases.spec.ts"
  "realtime_updates.spec.ts"
  "worker_isolation.spec.ts"
  "workflow_execution.spec.ts"
)

# Helper: check if filename is in allowlist
is_in_allowlist() {
  local filename="$1"
  shift
  local arr=("$@")
  for allowed in "${arr[@]}"; do
    if [[ "$filename" == "$allowed" ]]; then
      return 0
    fi
  done
  return 1
}

echo "🔍 Checking for test anti-patterns..."
echo ""

# =============================================================================
# Check 1: window.confirm( in frontend src (excluding test files)
# =============================================================================
echo "1️⃣  Checking for window.confirm() usage in frontend src..."

CONFIRM_MATCHES=""
while IFS= read -r -d '' file; do
  # Skip test files
  case "$file" in
    *.test.ts|*.test.tsx|*/__tests__/*|*/test/*) continue ;;
  esac

  if grep -n 'window\.confirm(' "$file" 2>/dev/null; then
    CONFIRM_MATCHES="${CONFIRM_MATCHES}${file}
"
  fi
done < <(find "$FRONTEND_SRC" -type f \( -name "*.ts" -o -name "*.tsx" \) -print0 2>/dev/null)

if [ -n "$CONFIRM_MATCHES" ]; then
  echo "❌ Found window.confirm() usage in frontend src."
  echo "   Use the useConfirm() hook instead for testable confirmation dialogs."
  echo ""
  failed=1
else
  echo "   ✅ No window.confirm() found"
fi

# =============================================================================
# Check 2: alert( in frontend src (excluding test files)
# =============================================================================
echo "2️⃣  Checking for alert() usage in frontend src..."

ALERT_MATCHES=""
while IFS= read -r -d '' file; do
  # Skip test files
  case "$file" in
    *.test.ts|*.test.tsx|*/__tests__/*|*/test/*) continue ;;
  esac

  # Match alert( but not console.alert, someobj.alert, etc.
  # Look for standalone alert( or window.alert(
  if grep -En '(^|[^a-zA-Z0-9_.])alert\(' "$file" 2>/dev/null; then
    ALERT_MATCHES="${ALERT_MATCHES}${file}
"
  fi
done < <(find "$FRONTEND_SRC" -type f \( -name "*.ts" -o -name "*.tsx" \) -print0 2>/dev/null)

if [ -n "$ALERT_MATCHES" ]; then
  echo "❌ Found alert() usage in frontend src."
  echo "   Use a proper notification system instead of native alerts."
  echo ""
  failed=1
else
  echo "   ✅ No alert() found"
fi

# =============================================================================
# Check 3: waitForTimeout( in E2E spec files
# =============================================================================
echo "3️⃣  Checking for waitForTimeout() in active E2E tests..."

violations_timeout=""

# Find all spec files with waitForTimeout
while IFS= read -r -d '' file; do
  # Skip helper directories
  case "$file" in
    */helpers/*) continue ;;
  esac

  # Check if file has waitForTimeout
  if ! grep -q 'waitForTimeout(' "$file" 2>/dev/null; then
    continue
  fi

  # Check if the entire file is skipped
  # Patterns that skip entire file: test.skip(); at start of line, or describe.skip(
  if grep -qE '^test\.skip\(\);?$' "$file" 2>/dev/null; then
    continue
  fi
  if grep -qE '^describe\.skip\(' "$file" 2>/dev/null; then
    continue
  fi

  # Check if file is in allowlist (grandfathered violations)
  filename=$(basename "$file")
  if is_in_allowlist "$filename" "${ALLOWLIST_TIMEOUT[@]}"; then
    continue
  fi

  # File has active tests - report the violation
  timeout_lines=$(grep -n 'waitForTimeout(' "$file" 2>/dev/null || true)
  if [ -n "$timeout_lines" ]; then
    violations_timeout="${violations_timeout}${file}:
${timeout_lines}

"
  fi
done < <(find "$E2E_TESTS" -type f -name "*.spec.ts" -print0 2>/dev/null)

if [ -n "$violations_timeout" ]; then
  echo "❌ Found waitForTimeout() in active E2E tests."
  echo "   Use deterministic waits instead (waitFor, waitForSelector, expect.poll)."
  echo "   If the test is intentionally slow/flaky, mark it with test.skip() at file level."
  echo ""
  echo "$violations_timeout"
  failed=1
else
  echo "   ✅ No waitForTimeout() in active tests"
fi

# =============================================================================
# Check 4: networkidle in E2E spec files
# =============================================================================
echo "4️⃣  Checking for networkidle in active E2E tests..."

violations_networkidle=""

# Find all spec files with networkidle
while IFS= read -r -d '' file; do
  # Skip helper directories
  case "$file" in
    */helpers/*) continue ;;
  esac

  # Check if file has actual networkidle usage (not just comments)
  if ! grep -qE "waitForLoadState.*networkidle|waitUntil.*networkidle" "$file" 2>/dev/null; then
    continue
  fi

  # Check if the entire file is skipped
  if grep -qE '^test\.skip\(\);?$' "$file" 2>/dev/null; then
    continue
  fi
  if grep -qE '^describe\.skip\(' "$file" 2>/dev/null; then
    continue
  fi

  # Check if file is in allowlist (grandfathered violations)
  filename=$(basename "$file")
  if is_in_allowlist "$filename" "${ALLOWLIST_NETWORKIDLE[@]}"; then
    continue
  fi

  # File has active tests - report the violation
  networkidle_lines=$(grep -n 'networkidle' "$file" 2>/dev/null || true)
  if [ -n "$networkidle_lines" ]; then
    violations_networkidle="${violations_networkidle}${file}:
${networkidle_lines}

"
  fi
done < <(find "$E2E_TESTS" -type f -name "*.spec.ts" -print0 2>/dev/null)

if [ -n "$violations_networkidle" ]; then
  echo "❌ Found networkidle in active E2E tests."
  echo "   networkidle is unreliable and causes flaky tests."
  echo "   Use waitForSelector or waitFor with specific conditions instead."
  echo "   If the test is intentionally slow/flaky, mark it with test.skip() at file level."
  echo ""
  echo "$violations_networkidle"
  failed=1
else
  echo "   ✅ No networkidle in active tests"
fi

# =============================================================================
# Summary
# =============================================================================
echo ""
if [ "$failed" -eq 0 ]; then
  echo "✅ No test anti-patterns detected"
  exit 0
else
  echo "❌ Test anti-patterns found. Please fix the issues above."
  exit 1
fi
