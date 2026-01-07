# E2E Tests (Playwright)

Playwright E2E tests for the unified Swarmlet SPA (`/`, `/dashboard`, `/chat`) and the Zerg backend.

## Run (recommended)

From repo root:

```bash
make test-e2e
make test-e2e-ui
make test-e2e-single TEST=tests/unified-frontend.spec.ts
make test-e2e-grep GREP="Jarvis"
```

## How it works

- Playwright starts an isolated backend (`apps/zerg/e2e/spawn-test-backend.js`) and a frontend dev server.
- Default ports are `BACKEND_PORT=8001` and `FRONTEND_PORT=8002` (override via env).
- Database isolation is per-Playwright-worker (Postgres schema per worker, routed by the `X-Test-Worker` header).

## Setup

```bash
# JS deps (repo root)
bun install

# Python deps (backend)
cd apps/zerg/backend && uv sync

# Playwright browser deps (from this folder)
cd apps/zerg/e2e && bunx playwright install
```

## Useful files

- `apps/zerg/e2e/playwright.config.js` — ports, web servers, reporters
- `apps/zerg/e2e/spawn-test-backend.js` — starts backend for tests (uv + uvicorn)
- `apps/zerg/e2e/tests/unified-frontend.spec.ts` — quick smoke suite
- `apps/zerg/e2e/tests/chat_*.spec.ts` — Jarvis chat-focused tests

## Reports

```bash
cd apps/zerg/e2e
bunx playwright show-report
```
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
- **Database logs**: PostgreSQL query logs (if enabled)
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
