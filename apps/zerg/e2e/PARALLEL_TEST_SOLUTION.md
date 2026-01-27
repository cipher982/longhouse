# E2E Parallel Test Execution Solution

## Problem

When running E2E tests in parallel with multiple commis, tests were failing due to:

- Shared database state between commis
- Race conditions when multiple tests modify data simultaneously
- Tests expecting exact row counts that varied due to concurrent operations

## Solution Implemented

We've implemented serial execution as a quick fix by modifying `playwright.config.js`:

- Set `fullyParallel: false`
- Set `commis: 1`

This ensures tests run one at a time, avoiding database conflicts.

## Results

- Failures reduced from 18 to 11
- Remaining failures are unrelated to parallelization (UI timing, incorrect expectations, missing features)

## Future Improvements

### Option 1: Commis-Isolated Databases (Recommended for true parallelism)

1. Modify backend to accept commis ID via environment variable
2. Create separate SQLite database per commis
3. Clean up commis databases after test run

### Option 2: Test Data Namespacing

1. Generate unique identifiers for all test data
2. Filter assertions by commis-specific data
3. Avoid exact count assertions

### Option 3: Hybrid Approach

1. Keep database-heavy tests serial
2. Run read-only or isolated tests in parallel
3. Use Playwright's test.describe.serial() for specific suites

## Remaining Test Issues (Not parallelization-related)

1. **Edit fiche name** - WebSocket update timing issue
2. **Dashboard empty state** - Text mismatch ("Create New Fiche" vs "Create Fiche")
3. **Canvas operations** - Feature not fully implemented
4. **Thread chat persistence** - Message history not persisting correctly
5. **Webhook triggers** - UI elements not found

These should be addressed separately as they're application bugs, not test infrastructure issues.
