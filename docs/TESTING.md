# Testing Guide

**CRITICAL:** Always use Make targets. Never run pytest/bun/playwright directly (they miss env vars, wrong CWD, no isolation).

## Commands

| Command | What | Notes |
|---------|------|-------|
| `make test` | Unit tests (backend + frontend) | ~50 lines |
| `make test MINIMAL=1` | Unit tests (compact) | **Recommended for agents** |
| `make test-e2e-core` | Core E2E (critical path) | **No retries**, must pass 100% |
| `make test-e2e` | Full E2E (minus core) | Retries allowed |
| `make test-all` | Unit + full E2E | Does not include core |
| `make test-e2e-single TEST=<spec>` | Single spec | Most useful for iteration |
| `make test-e2e-verbose` | E2E with full output | For debugging |
| `make test-e2e-errors` | Show last errors | `cat test-results/errors.txt` |
| `make test-e2e-query Q='...'` | Query results JSON | e.g., `Q='.failed[]'` |

**Full E2E coverage** = run **both** `make test-e2e-core` and `make test-e2e`.

## E2E Output

Minimal reporter designed for AI agents (~10 lines pass, ~30 fail):

```
✓ E2E: 332 passed (8m 32s)
```

On failure:
```
✗ E2E: 45 passed, 245 failed (10m 49s)
  tests/chat.spec.ts:45 "sends message"
  ... and 243 more

→ Errors: cat test-results/errors.txt
→ Query:  jq '.failed[]' test-results/summary.json
```

**Files generated** (in `apps/zerg/e2e/test-results/`):
- `summary.json` — Query with `jq`
- `errors.txt` — Human-readable errors
- `full-output.log` — All suppressed logs

## Debugging Failures

1. Check summary: `make test-e2e-query Q='.failed[] | .file'`
2. Read errors: `make test-e2e-errors`
3. Re-run single: `make test-e2e-single TEST=tests/chat.spec.ts`
4. Full verbose: `make test-e2e-verbose`
5. Interactive: `make test-e2e-ui`

## Test Isolation

- Per-worker Postgres schemas (not SQLite)
- 8 Playwright workers + 8 uvicorn workers
- Database reset with retry logic + stagger delays
- Artifacts in `apps/zerg/e2e/test-results/` and `playwright-report/`
- Override: `PLAYWRIGHT_WORKERS=N make test-e2e`

## E2E Gotchas

- **E2E runs on insecure origin**: Use `src/jarvis/lib/uuid.ts` not `crypto.randomUUID()`
- **WebRTC tests**: Skip at describe-level when `SKIP_WEBRTC_TESTS=true`
- **Deterministic UI tests**: Emit events via `window.__jarvis.eventBus` (DEV only)
- **Test isolation issues**: Try `make test-e2e-reset`
