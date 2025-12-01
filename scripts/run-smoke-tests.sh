#!/bin/bash

# Quick Smoke Test - Tests basic functionality without visual comparison
# Zero human interaction required

set -e

echo "💨 Quick Smoke Test Starting..."
echo "═══════════════════════════════════════════════════════════════════════════════"

# Ensure we're in project root
cd "$(dirname "$0")/.."

# Kill any existing processes on our ports first
echo "🧹 Cleaning up existing processes..."
pkill -f "port=47300" || true
pkill -f "port=47200" || true
sleep 2

echo "🚀 Starting backend service..."

# Start backend in the background
cd apps/zerg/backend && uv run python -m uvicorn zerg.main:app --host=127.0.0.1 --port=47300 --log-level=warning > ../../../test-backend.log 2>&1 &
BACKEND_PID=$!
cd ../../..

# Give backend time to start
echo "  ⏳ Waiting for backend to initialize..."
sleep 5

# Verify backend is responding
if ! curl -s http://localhost:47300/health > /dev/null; then
    echo "❌ Backend failed to start!"
    kill $BACKEND_PID || true
    exit 1
fi

echo "  ✅ Backend ready"

# Function to cleanup on exit
cleanup() {
    echo "🧹 Cleaning up..."
    kill $BACKEND_PID || true
    rm -f test-backend.log
}
trap cleanup EXIT

# Navigate to e2e directory
cd apps/zerg/e2e

# Just test that we can reach the dashboards in both UIs
echo ""
echo "🎯 Testing Basic UI Availability..."
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Create a simple smoke test on the fly
cat > smoke-test.js << 'EOF'
const { chromium } = require('@playwright/test');
const fs = require('fs');

async function testUI(useRust) {
    const browser = await chromium.launch();
    const context = await browser.newContext();
    const page = await context.newPage();

    try {
        // Start appropriate frontend
        if (useRust) {
            // Test rust frontend directly (if available)
            await page.goto('http://localhost:47200/', { waitUntil: 'networkidle' });
            await page.waitForSelector('#dashboard-container', { timeout: 10000 });
        } else {
            // Test react frontend
            await page.goto('http://localhost:47200/react/index.html', { waitUntil: 'networkidle' });
            await page.waitForSelector('[data-testid="dashboard-container"]', { timeout: 10000 });
        }

        console.log(`✅ ${useRust ? 'Rust' : 'React'} UI loaded successfully`);
        return true;
    } catch (error) {
        console.log(`❌ ${useRust ? 'Rust' : 'React'} UI failed: ${error.message}`);
        return false;
    } finally {
        await browser.close();
    }
}

async function main() {
    const reactResult = await testUI(false);
    const rustResult = await testUI(true);

    if (reactResult && rustResult) {
        console.log('\n✨ Both UIs are accessible!');
        process.exit(0);
    } else if (reactResult) {
        console.log('\n👍 React UI is working (Rust may not be built)');
        process.exit(0);
    } else {
        console.log('\n❌ UI accessibility issues detected');
        process.exit(1);
    }
}

main();
EOF

# Install playwright if needed
if [ ! -d "node_modules" ]; then
    echo "📦 Installing dependencies..."
    npm install --silent
fi

# Run the smoke test
echo "  🧪 Testing UI accessibility..."
node smoke-test.js

# Clean up temporary test file
rm -f smoke-test.js

cd ..

echo ""
echo "💨 Smoke test complete!"
echo "═══════════════════════════════════════════════════════════════════════════════"