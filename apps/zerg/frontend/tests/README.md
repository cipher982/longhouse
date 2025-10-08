# Frontend Integration Tests

This directory contains integration tests for the frontend, including:

## Pact Contract Coverage Test

The `pact_contract_coverage.rs` test ensures that all WebSocket message types defined in the `WsMessage` enum have corresponding Pact contracts.

### What it does

1. **Parses the WsMessage enum** - Extracts all message type names from `src/network/ws_schema.rs`, including serde rename and alias attributes
2. **Parses contract definitions** - Extracts all message types that have Pact contracts defined in `src/bin/contract_capture.rs`
3. **Compares the lists** - Identifies:
   - Message types missing contracts
   - Orphaned contracts (contracts without corresponding message types)

### Running the tests

```bash
# Run the coverage test (fails if any contracts are missing)
cargo test --test pact_contract_coverage test_all_ws_message_types_have_contracts

# Generate a detailed coverage report
cargo test --test pact_contract_coverage generate_pact_coverage_report -- --nocapture

# Use the convenience script
./check_pact_coverage.sh
```

### Interpreting the results

The coverage report shows:
- Total message types and contracts
- Coverage percentage
- A table showing which message types have contracts
- Any orphaned contracts
- Example contract templates for missing message types

### Adding new message types

When you add a new variant to the `WsMessage` enum:

1. The test will automatically detect it
2. Add a corresponding interaction to `contract_capture.rs`
3. The test will pass once the contract is added

### Excluding message types

If certain message types should not have contracts (e.g., client-only messages), add them to the `exceptions` set in the test.

### Maintenance

This test is self-maintaining - it automatically detects new message types and will fail CI builds if contracts are not added, ensuring the contract coverage stays up to date.