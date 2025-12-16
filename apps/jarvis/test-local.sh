#!/bin/bash
# Local Jarvis Testing Script
# Run tests locally without Docker for faster iteration
#
# Usage:
#   ./test-local.sh unit           # Run unit tests
#   ./test-local.sh unit-watch     # Run unit tests in watch mode
#   ./test-local.sh e2e            # Run E2E tests with visible browser
#   ./test-local.sh e2e-debug      # Run E2E tests with debugger
#   ./test-local.sh e2e [test-name] # Run specific E2E test

set -e

case "$1" in
  "unit")
    echo "🧪 Running unit tests..."
    cd apps/web
    npm test -- --run
    ;;

  "unit-watch")
    echo "🧪 Running unit tests in watch mode..."
    cd apps/web
    npm test -- --watch
    ;;

  "e2e")
    echo "🧪 Running E2E tests locally (visible browser)..."
    echo "⚠️  Make sure zerg-backend and jarvis-web are running!"
    echo "   Start with: make zerg (from repo root)"
    echo ""

    # Check if services are running
    if ! curl -sf http://localhost:47300/api/jarvis/session > /dev/null 2>&1; then
      echo "❌ zerg-backend not running (or /api/jarvis/session not responding)"
      echo "   Start with: make zerg (from repo root)"
      exit 1
    fi

    if ! curl -sf http://localhost:8080 > /dev/null 2>&1; then
      echo "❌ jarvis-web not running on port 8080"
      echo "   Start with: cd apps/jarvis/apps/web && bun run dev"
      exit 1
    fi

    echo "✅ Services running, starting E2E tests..."

    if [ -n "$2" ]; then
      # Run specific test
      npx playwright test "$2" --headed
    else
      # Run all tests
      npx playwright test --headed
    fi
    ;;

  "e2e-debug")
    echo "🧪 Running E2E tests in debug mode..."
    npx playwright test --debug
    ;;

  "e2e-ui")
    echo "🧪 Running E2E tests with interactive UI..."
    npx playwright test --ui
    ;;

  *)
    echo "Usage: ./test-local.sh {unit|unit-watch|e2e|e2e-debug|e2e-ui} [test-name]"
    echo ""
    echo "Examples:"
    echo "  ./test-local.sh unit                    # Fast unit tests"
    echo "  ./test-local.sh unit-watch              # Watch mode for TDD"
    echo "  ./test-local.sh e2e                     # E2E with visible browser"
    echo "  ./test-local.sh e2e text-message        # Run specific E2E test"
    echo "  ./test-local.sh e2e-debug               # Step through tests"
    echo "  ./test-local.sh e2e-ui                  # Interactive test UI"
    exit 1
    ;;
esac
