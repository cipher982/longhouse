# E2E Tests (Playwright)

Playwright E2E tests for the React dashboard + chat (`/dashboard`, `/fiche/...`) and the Zerg backend.

## Run (recommended)

From repo root:

```bash
make test-e2e        # core + a11y (core runs serially)
make test-zerg-e2e   # full suite (non-core)
make test-e2e-ui
make test-e2e-single TEST=tests/unified-frontend.spec.ts
make test-e2e-grep GREP="Oikos"
```

## How it works

- Playwright starts an isolated backend (`apps/zerg/e2e/spawn-test-backend.js`) and a frontend dev server.
- Default ports are `BACKEND_PORT=8001` and `FRONTEND_PORT=8002` (override via env).
- Database isolation is per-Playwright-commis **SQLite file** (routed by `X-Test-Commis` header + `commis` ws param).
- SQLite files live under `$E2E_DB_DIR` (temp dir) and are cleaned in global teardown.
- Core suite runs with `--workers=1` because SQLite resets + commis jobs can race under parallelism.

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
- `apps/zerg/e2e/tests/fixtures.ts` — injects `X-Test-Commis` and websocket `commis` param
- `apps/zerg/e2e/tests/unified-frontend.spec.ts` — quick smoke suite
- `apps/zerg/e2e/tests/chat_*.spec.ts` — Oikos chat-focused tests

## Reports

```bash
cd apps/zerg/e2e
bunx playwright show-report
```

### Update Test Runner (if needed)

Add to `run_e2e_tests.sh`:

```bash
# In get_test_files function
basic)
    echo "existing_tests.spec.ts my_new_feature.spec.ts"
    ;;
```

### Use Helper Libraries

- Import from `./helpers/` directory
- Use consistent patterns for commis ID handling
- Leverage existing utilities for common operations

## 🐛 Debugging

### Common Issues

1. **Database isolation**: Use `testInfo.parallelIndex` (commisIndex can exceed configured commis count)
2. **Health checks**: Use `/api/system/health` (Vite only proxies `/api/*`)
3. **Server startup**: Check ports 8001/8002 are available
4. **Element timing**: Use `waitForStableElement()` for dynamic content
5. **Test cleanup**: Verify database reset between tests

### Debug Commands

```bash
# Run single test with debugging
npx playwright test tests/fiche_creation_full.spec.ts --debug

# Run with browser visible
npx playwright test --headed

# Generate trace files
npx playwright test --trace on
```

### Log Analysis

- **Test output**: Console logs with timestamps
- **Playwright traces**: Visual debugging in browser
- **Database logs**: SQLite files under `$E2E_DB_DIR`
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
2. **Use consistent commis ID handling** via `testInfo.parallelIndex`
3. **Add comprehensive logging** for debugging
4. **Include error handling** for flaky operations
5. **Document new patterns** in helper libraries

---

**Architecture Status**: ✅ SQLite isolation working, ✅ Commis management stable, ✅ Helper libraries consolidated
