#!/bin/bash

# Visual Analysis Runner
# Comprehensive UI parity testing with AI-powered analysis

set -e

echo "🎨 Comprehensive Visual Analysis System"
echo "═══════════════════════════════════════════════════════════════════════════════"
echo ""

# Ensure we're in project root
cd "$(dirname "$0")/.."

# Configuration
BACKEND_PORT=47300
FRONTEND_PORT=47200
CLEANUP_ON_EXIT=true

# Parse command line arguments
PAGES="all"
HEADLESS="true"
VERBOSE="false"

while [[ $# -gt 0 ]]; do
    case $1 in
        --pages=*)
            PAGES="${1#*=}"
            shift
            ;;
        --headed)
            HEADLESS="false"
            shift
            ;;
        --verbose|-v)
            VERBOSE="true"
            shift
            ;;
        --no-cleanup)
            CLEANUP_ON_EXIT="false"
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --pages=PAGES     Specific pages to test (dashboard,chat,canvas or 'all')"
            echo "  --headed          Run tests with visible browser"
            echo "  --verbose         Show detailed output"
            echo "  --no-cleanup      Don't kill background services on exit"
            echo "  --help            Show this help message"
            echo ""
            echo "Examples:"
            echo "  $0                          # Test all pages, headless"
            echo "  $0 --pages=dashboard --headed  # Test only dashboard with visible browser"
            echo "  $0 --pages=chat,canvas         # Test specific pages"
            echo ""
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

echo "🔧 Configuration:"
echo "  Pages: $PAGES"
echo "  Headless: $HEADLESS"
echo "  Verbose: $VERBOSE"
echo ""

# Function to cleanup on exit
cleanup() {
    if [ "$CLEANUP_ON_EXIT" = "true" ]; then
        echo "🧹 Cleaning up background services..."
        pkill -f "port=$BACKEND_PORT" || true
        pkill -f "port=$FRONTEND_PORT" || true
    fi
}
trap cleanup EXIT

# Kill any existing processes on our ports
echo "🧹 Cleaning up existing processes..."
pkill -f "port=$BACKEND_PORT" || true
pkill -f "port=$FRONTEND_PORT" || true
sleep 2

echo "🚀 Starting services..."

# Start backend in testing mode
echo "  📡 Starting backend on port $BACKEND_PORT..."
cd apps/zerg/backend && TESTING=1 ENVIRONMENT=test:e2e NODE_ENV=test uv run python -m uvicorn zerg.main:app \
    --host=127.0.0.1 --port=$BACKEND_PORT --log-level=warning > ../../../visual-backend.log 2>&1 &
BACKEND_PID=$!
cd ../../..

# Start React frontend
echo "  ⚛️  Starting React frontend on port $FRONTEND_PORT..."
cd apps/zerg/frontend-web && bun run dev -- --host 127.0.0.1 --port $FRONTEND_PORT > ../../../visual-frontend.log 2>&1 &
FRONTEND_PID=$!
cd ../../..

# Wait for services to start
echo "  ⏳ Waiting for services to initialize..."
sleep 8

# Verify services are responding
echo "  🔍 Verifying services..."
if ! curl -s http://localhost:$BACKEND_PORT/health > /dev/null; then
    echo "❌ Backend failed to start on port $BACKEND_PORT!"
    echo "Backend logs:"
    cat visual-backend.log 2>/dev/null || echo "No backend logs available"
    exit 1
fi

if ! curl -s http://localhost:$FRONTEND_PORT/ > /dev/null; then
    echo "❌ Frontend failed to start on port $FRONTEND_PORT!"
    echo "Frontend logs:"
    cat visual-frontend.log 2>/dev/null || echo "No frontend logs available"
    exit 1
fi

echo "  ✅ Services ready"
echo ""

# Navigate to E2E directory
cd apps/zerg/e2e

# Prepare Playwright command
PLAYWRIGHT_CMD="bunx playwright test comprehensive-visual-test.ts"

# Add headless/headed mode
if [ "$HEADLESS" = "false" ]; then
    PLAYWRIGHT_CMD="$PLAYWRIGHT_CMD --headed"
fi

# Add verbosity
if [ "$VERBOSE" = "true" ]; then
    PLAYWRIGHT_CMD="$PLAYWRIGHT_CMD --reporter=line"
else
    PLAYWRIGHT_CMD="$PLAYWRIGHT_CMD --reporter=dot"
fi

# Filter by specific pages if requested
if [ "$PAGES" != "all" ]; then
    # Convert comma-separated pages to test filter
    PAGES_FILTER=$(echo "$PAGES" | sed 's/,/|/g')
    PLAYWRIGHT_CMD="$PLAYWRIGHT_CMD --grep \"($PAGES_FILTER)\""
fi

echo "🎨 Running visual analysis tests..."
echo "Command: $PLAYWRIGHT_CMD"
echo ""

# Run the tests
if eval $PLAYWRIGHT_CMD; then
    echo ""
    echo "✅ Visual analysis completed successfully!"
    echo ""
    echo "📊 Results:"
    echo "  📄 Test reports: e2e/test-results/"
    echo "  📄 Visual reports: e2e/visual-reports/"
    echo "  📸 Screenshots: attached to test results"
    echo ""
    echo "🔍 To view detailed HTML report:"
    echo "  cd e2e && bunx playwright show-report"
    echo ""
else
    echo ""
    echo "❌ Visual analysis encountered issues!"
    echo ""
    echo "🔍 Troubleshooting:"
    echo "  1. Check service logs: visual-backend.log, visual-frontend.log"
    echo "  2. Verify OpenAI API key is set: echo \$OPENAI_API_KEY"
    echo "  3. Run with --verbose for detailed output"
    echo "  4. View test report: cd e2e && bunx playwright show-report"
    echo ""
    exit 1
fi

# Clean up log files
rm -f visual-backend.log visual-frontend.log 2>/dev/null || true

echo "🎉 Comprehensive visual analysis complete!"
echo "═══════════════════════════════════════════════════════════════════════════════"