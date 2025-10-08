#!/bin/bash
# Script to check Pact contract coverage for WebSocket messages

echo "🔍 Checking Pact contract coverage..."
echo ""

# Run the coverage test
if cargo test --test pact_contract_coverage test_all_ws_message_types_have_contracts -- --quiet 2>/dev/null; then
    echo "✅ All WebSocket message types have corresponding Pact contracts!"
else
    echo "❌ Pact contract coverage check failed!"
    echo ""
    echo "Running detailed coverage report..."
    echo ""
    cargo test --test pact_contract_coverage generate_pact_coverage_report -- --nocapture --quiet
    exit 1
fi