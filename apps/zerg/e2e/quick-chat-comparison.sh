#!/bin/bash

# Quick Chat UI Comparison Test
# Runs focused chat tests against both Rust and React UIs

set -e

cd "$(dirname "$0")"

echo "🚀 Quick Chat UI Comparison Test"
echo "================================"
echo ""

# Ensure servers are running
if ! curl -s http://localhost:47300/health > /dev/null 2>&1; then
    echo "⚠️  Backend not running on port 47300. Starting servers..."
    cd ..
    make start &
    sleep 5
    cd e2e
fi

# Install dependencies if needed
if [ ! -d "node_modules" ]; then
    echo "📦 Installing E2E dependencies..."
    npm install
fi

echo "🧪 Testing Chat Functionality..."
echo ""

# Run tests against Rust UI
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🦀 RUST/WASM UI"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
PLAYWRIGHT_USE_RUST_UI=1 npx playwright test tests/chat_functional.spec.ts \
    --grep "Send message and verify it appears" \
    --reporter=list || RUST_RESULT=$?

echo ""

# Run tests against React UI
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "⚛️  REACT UI"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
PLAYWRIGHT_USE_RUST_UI=0 npx playwright test tests/chat_functional.spec.ts \
    --grep "Send message and verify it appears" \
    --reporter=list || REACT_RESULT=$?

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 RESULTS SUMMARY"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

if [ "${RUST_RESULT:-0}" -eq 0 ]; then
    echo "✅ Rust UI:  PASSED"
else
    echo "❌ Rust UI:  FAILED"
fi

if [ "${REACT_RESULT:-0}" -eq 0 ]; then
    echo "✅ React UI: PASSED"
else
    echo "❌ React UI: FAILED"
fi

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# Return success only if both passed
if [ "${RUST_RESULT:-0}" -eq 0 ] && [ "${REACT_RESULT:-0}" -eq 0 ]; then
    echo "🎉 Both UIs passed the chat test!"
    exit 0
else
    echo "⚠️  Some tests failed. Check output above."
    exit 1
fi