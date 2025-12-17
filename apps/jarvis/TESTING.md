# Jarvis Testing Quick Reference

Fast iteration guide for Jarvis testing. Get results in seconds, not minutes.

## Quick Start (What You Probably Want)

```bash
# Fast unit tests (2-3 seconds)
make test-jarvis-unit

# Watch mode for TDD (auto-rerun on save)
make test-jarvis-watch

# Run specific E2E test (with hot reload)
make test-jarvis-text        # Text message flow
make test-jarvis-history     # History hydration
```

## Speed Comparison

| Method                   | Time    | Use When                               |
| ------------------------ | ------- | -------------------------------------- |
| `make test-jarvis-unit`  | ~3 sec  | Unit tests, quick feedback             |
| `make test-jarvis-watch` | instant | TDD, iterating on logic                |
| `make test-jarvis-text`  | ~10 sec | Specific E2E test (hot reload enabled) |
| `make test-jarvis-e2e`   | ~2 min  | Full E2E suite                         |
| `make test-jarvis` (old) | ~2 min  | CI/pre-commit                          |

## All Available Commands

### Unit Tests (No Docker)

```bash
make test-jarvis-unit        # Run all unit tests
make test-jarvis-watch       # Watch mode (TDD)
./test-local.sh unit         # Alternative syntax
./test-local.sh unit-watch   # Alternative syntax
```

### E2E Tests (Docker)

```bash
make test-jarvis-e2e         # All E2E tests
make test-jarvis-text        # Text message tests only
make test-jarvis-history     # History hydration tests only
make test-jarvis-grep GREP="should send"  # Specific test by name
```

### E2E Tests (Local - Visible Browser)

```bash
./test-local.sh e2e                    # All tests with browser
./test-local.sh e2e text-message       # Specific test
./test-local.sh e2e-debug              # Step through with debugger
./test-local.sh e2e-ui                 # Interactive UI
```

**Requirements for local E2E**:

1. Start zerg-backend (Jarvis BFF): `make zerg` (or `make dev` for full stack)
2. Start jarvis-web: `cd apps/web && npm run dev`
3. Then run tests

## Hot Reload Setup

**Key improvement**: Source code is now volume-mounted in Docker, so code changes reload automatically.

**Before**: Edit code → rebuild image (2-3 min) → run test
**After**: Edit code → Vite reloads (~2 sec) → rerun test

### How it works

- `docker-compose.test.yml` mounts `src/`, `lib/`, and `tests/` directories
- Vite dev server watches for changes and hot-reloads
- No rebuild needed for code changes
- Only rebuild for dependency changes

### When you still need to rebuild

```bash
# Only when you change package.json or Dockerfile
docker compose -f docker-compose.test.yml build jarvis-web
```

## Debugging Failed Tests

### 1. Check test output

```bash
make test-jarvis-text  # Runs in Docker, see console
```

Note: Jarvis no longer uses a separate `jarvis-server` service. The Jarvis BFF
and OpenAI Realtime proxy are served by `zerg-backend` at `/api/jarvis/*`.

### 2. Run locally with visible browser

```bash
# Start services
cd apps/server && npm run dev   # Terminal 1
cd apps/web && npm run dev      # Terminal 2

# Run test with browser visible
cd apps/jarvis
./test-local.sh e2e text-message
```

### 3. Use debugger

```bash
./test-local.sh e2e-debug
```

Then click "Resume" through test steps, inspect DOM, set breakpoints.

### 4. Check screenshots/traces

Failed tests automatically save:

- `test-results/[test-name]/test-failed-1.png` - Screenshot at failure
- `test-results/[test-name]/trace.zip` - Full trace (view with `npx playwright show-trace`)

## Common Patterns

### Test a specific file quickly

```bash
make test-jarvis-grep GREP="Text Message Happy Path"
```

### Iterate on a failing test

```bash
# Terminal 1: Keep services running
docker compose -f docker-compose.test.yml up zerg-backend jarvis-web

# Terminal 2: Edit code, rerun test (instant reload)
make test-jarvis-text
# Edit code...
make test-jarvis-text  # Rerun (hot reload, no rebuild)
```

### Debug why test fails

```bash
# Option 1: Run locally with visible browser
./test-local.sh e2e text-message

# Option 2: Use interactive UI
./test-local.sh e2e-ui
# Click through tests, see live browser, inspect DOM
```

## Tips

1. **Start with unit tests**: Fastest feedback loop
2. **Use watch mode**: Automatic rerun on save
3. **Hot reload E2E**: Code changes reload automatically in Docker
4. **Run locally for debugging**: See the browser, inspect console
5. **Use grep for specific tests**: Don't run the whole suite

## Architecture

```
apps/jarvis/
├── apps/web/
│   ├── src/           # Source code (volume mounted)
│   ├── lib/           # Controllers (volume mounted)
│   └── tests/         # Unit tests (vitest)
├── tests/             # E2E tests (Playwright, volume mounted)
├── docker-compose.test.yml  # Test environment
└── test-local.sh      # Local testing helper
```

## Troubleshooting

### "No tests found"

- Check test file path is correct
- Playwright uses glob patterns, not grep in filename

### E2E tests timeout

- Check if services are healthy: `docker compose -f docker-compose.test.yml ps`
- Check logs: `docker compose -f docker-compose.test.yml logs jarvis-web`

### Hot reload not working

- Verify volumes are mounted: `docker compose -f docker-compose.test.yml config`
- Check Vite is watching: Should see "hmr update" in logs

### Tests fail locally but pass in Docker (or vice versa)

- Check `.env` files differ
- Check API keys are set
- Local uses localhost:8080, Docker uses jarvis-web:8080
