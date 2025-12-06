# End-to-End Tests

This directory contains the comprehensive E2E test suite for the Zerg Agent Platform using Playwright with advanced database isolation and real-time testing capabilities.

## 🚀 Quick Start

### From Root Directory (Recommended)

```bash
# Run basic E2E tests (fast validation)
make e2e-basic

# Run full E2E test suite
make e2e-full
```

### Direct Commands

```bash
# From the e2e directory
./run_e2e_tests.sh --mode=basic     # ~3 min - core functionality
./run_e2e_tests.sh --mode=full      # ~15 min - comprehensive suite

# Manual Playwright commands
npm test                            # Run all tests
npm run test:headed                 # Run with browser visible
npm run test:debug                  # Interactive debugging
```

## 📁 Current Test Structure

```
e2e/
├── tests/                              # Test specifications
│   ├── agent_creation_full.spec.ts     # Agent lifecycle (basic mode)
│   ├── comprehensive_debug.spec.ts     # System diagnostics (basic mode)
│   ├── canvas_complete_workflow.spec.ts # Canvas workflow (basic mode)
│   ├── workflow_execution_http.spec.ts # HTTP tool execution
│   ├── tool_palette_node_connections.spec.ts # Node connections
│   ├── error_handling_edge_cases.spec.ts     # Error scenarios
│   ├── data_persistence_recovery.spec.ts     # Data integrity
│   ├── performance_load_testing.spec.ts      # Performance tests
│   ├── accessibility_ui_ux.spec.ts           # Accessibility compliance
│   ├── multi_user_concurrency.spec.ts       # Multi-user scenarios
│   ├── realtime_websocket_monitoring.spec.ts # WebSocket testing
│   └── helpers/                             # Test utilities
│       ├── api-client.ts                    # API interaction layer
│       ├── agent-helpers.ts                 # Agent lifecycle helpers
│       ├── database-helpers.ts              # Database management
│       ├── canvas-helpers.ts                # Canvas interaction helpers
│       ├── test-helpers.ts                  # Common test utilities
│       ├── test-utils.ts                    # Utility functions
│       ├── workflow-helpers.ts              # Workflow operations
│       └── debug-helpers.ts                 # Debugging utilities
├── fixtures.ts                             # Playwright fixtures with worker isolation
├── playwright.config.js                    # Playwright configuration
├── run_e2e_tests.sh                        # Unified test runner
├── package.json                            # Dependencies and scripts
└── README.md                               # This file
```

## 🔧 Architecture & Features

### Advanced Database Isolation

- **Per-worker SQLite databases** (`test_worker_{id}.db`)
- **Automatic header injection** via fixtures (`X-Test-Worker: {workerId}`)
- **WebSocket worker isolation** with query parameters
- **Clean database state** between test runs

### Automated Server Management

- **Backend auto-start** on port 8001 (FastAPI + SQLite)
- **Frontend auto-start** on port 8002 (WASM server)
- **Parallel test execution** with 2 workers
- **Automatic cleanup** of test databases

### Real-time Testing

- **WebSocket connection testing** with worker isolation
- **Event monitoring** and validation
- **UI synchronization** verification
- **Cross-session communication** testing

## 📊 Test Modes

### Basic Mode (`--mode=basic`)

**Duration**: ~3 minutes
**Purpose**: Core functionality validation
**Tests**:

- `agent_creation_full.spec.ts` - Agent lifecycle with database isolation
- `comprehensive_debug.spec.ts` - System connectivity and health
- `canvas_complete_workflow.spec.ts` - Canvas navigation and workflow UI

### Full Mode (`--mode=full`)

**Duration**: ~15 minutes
**Purpose**: Comprehensive validation
**Tests**: All basic tests plus:

- Performance and load testing
- Accessibility compliance
- Multi-user concurrency
- Error handling edge cases
- Data persistence validation
- WebSocket real-time features

## 🛠️ Helper Libraries

### Database Helpers (`database-helpers.ts`)

```typescript
// Reset database for specific worker
await resetDatabaseForWorker(workerId);

// Ensure clean state before test
await ensureCleanDatabase(page, workerId);

// Verify database isolation
const isEmpty = await verifyDatabaseEmpty(workerId);
```

### Agent Helpers (`agent-helpers.ts`)

```typescript
// Create agent via API with defaults
const agent = await createAgentViaAPI(workerId, { name: "Test Agent" });

// Create multiple agents
const agents = await createMultipleAgents(workerId, { count: 3 });

// Create agent via UI
const agentId = await createAgentViaUI(page);

// Cleanup agents
await cleanupAgents(workerId, agents);
```

### Test Utils (`test-utils.ts`)

```typescript
// Get worker ID from test context
const workerId = getWorkerIdFromTest(testInfo);

// Retry with backoff
await retryWithBackoff(async () => {
  /* operation */
});

// Wait for stable element
await waitForStableElement(page, "#my-element");

// Safe navigation
await safeNavigate(page, "/dashboard");
```

## 🔍 Configuration Details

### Playwright Configuration

- **Base URL**: `http://localhost:8002` (frontend)
- **Backend URL**: `http://localhost:8001` (auto-started)
- **Workers**: 2 parallel workers
- **Timeout**: 30s (basic), 60s (full)
- **Retries**: 0 (dev), 2 (CI)

### Environment Variables

- `NODE_ENV=test` - Test environment
- `TESTING=1` - Testing mode flag
- `PW_TEST_WORKER_INDEX` - Worker identifier
- `E2E_LOG_SUPPRESS=1` - Reduce log noise

### Database Configuration

- **File Pattern**: `test_worker_{workerId}.db`
- **Location**: `/tmp/zerg_test_dbs/`
- **Cleanup**: Automatic after test completion
- **Isolation**: Per-worker via `X-Test-Worker` header

## 📈 Test Quality Features

### Comprehensive Logging

```
📊 [10:30:45] Agent creation test starting
📊 Worker ID: 0
📊 Initial agent count: 0
✅ Agent created via API: Test Agent (ID: 1)
📊 Updated agent count: 1
✅ Agent successfully appears in UI
```

### Error Handling

- **Automatic retries** for flaky operations
- **Graceful degradation** for missing UI elements
- **Detailed error context** in test reports
- **Screenshot capture** on failures

### Performance Monitoring

- **Page load time** measurement
- **API response time** tracking
- **Memory usage** monitoring
- **Network request** validation

## 🚦 Adding New Tests

### 1. Create Test File

```typescript
import { test, expect } from "./fixtures";
import { getWorkerIdFromTest } from "./helpers/test-utils";
import { createAgentViaAPI } from "./helpers/agent-helpers";

test.describe("My New Feature", () => {
  test("should work correctly", async ({ page }, testInfo) => {
    const workerId = getWorkerIdFromTest(testInfo);

    // Test implementation
    const agent = await createAgentViaAPI(workerId);
    // ... rest of test
  });
});
```

### 2. Update Test Runner (if needed)

Add to `run_e2e_tests.sh`:

```bash
# In get_test_files function
basic)
    echo "existing_tests.spec.ts my_new_feature.spec.ts"
    ;;
```

### 3. Use Helper Libraries

- Import from `./helpers/` directory
- Use consistent patterns for worker ID handling
- Leverage existing utilities for common operations

## 🐛 Debugging

### Common Issues

1. **Database isolation**: Ensure using `testInfo.workerIndex` for worker ID
2. **Server startup**: Check ports 8001/8002 are available
3. **Element timing**: Use `waitForStableElement()` for dynamic content
4. **Test cleanup**: Verify database reset between tests

### Debug Commands

```bash
# Run single test with debugging
npx playwright test tests/agent_creation_full.spec.ts --debug

# Run with browser visible
npx playwright test --headed

# Generate trace files
npx playwright test --trace on
```

### Log Analysis

- **Test output**: Console logs with timestamps
- **Playwright traces**: Visual debugging in browser
- **Database logs**: SQLite query logs (if enabled)
- **Network logs**: HTTP request/response details

## 📚 Dependencies

### Core Dependencies

- `@playwright/test` - Testing framework
- `@axe-core/playwright` - Accessibility testing
- `pixelmatch` - Visual comparison
- `pngjs` - Image processing

### Development Dependencies

- `prettier` - Code formatting
- `stylelint` - CSS linting

## 🎯 Success Metrics

- **Test Coverage**: 100% of critical user journeys
- **Database Isolation**: 0% cross-test contamination
- **Performance**: < 5s page loads, < 500ms API responses
- **Accessibility**: WCAG 2.1 AA compliance
- **Reliability**: < 1% flaky test rate

## 🤝 Contributing

1. **Follow existing patterns** in helper libraries
2. **Use consistent worker ID handling** via `testInfo.workerIndex`
3. **Add comprehensive logging** for debugging
4. **Include error handling** for flaky operations
5. **Document new patterns** in helper libraries

---

**Architecture Status**: ✅ Database isolation working, ✅ Worker management stable, ✅ Helper libraries consolidated
