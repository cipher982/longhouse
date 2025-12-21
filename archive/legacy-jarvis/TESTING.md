# Jarvis Testing (Unified SPA)

Jarvis chat UI is tested as part of the unified Zerg SPA.

- UI code: `apps/zerg/frontend-web/src/jarvis/`
- E2E tests: `apps/zerg/e2e/tests/`

## Common Commands

```bash
# Unit tests only (backend + frontend; no Playwright)
make test

# E2E (Playwright) for the unified SPA
make test-e2e

# Chat-specific E2E smoke tests (/chat)
make test-chat-e2e
```

## Debugging E2E

```bash
# UI mode
cd apps/zerg/e2e && bunx playwright test --ui

# Timeline + max-concurrency summary (useful for parallelism debugging)
cd apps/zerg/e2e && scripts/run_timeline.sh tests/unified-frontend.spec.ts
```
