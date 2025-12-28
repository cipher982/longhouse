# E2E Test Suite Overview

Playwright suite lives under `apps/zerg/e2e/tests/`. This file is a map, not a full catalog.

## Run

From repo root:

```bash
make test-e2e
make test-e2e-single TEST=tests/unified-frontend.spec.ts
make test-e2e-grep GREP="chat_"
make test-e2e-ui
```

## What to run while iterating

- `tests/unified-frontend.spec.ts` — unified SPA smoke tests (routing + basic UI)
- `tests/chat_functional.spec.ts` and `tests/chat_token_streaming.spec.ts` — Jarvis chat behavior
- `tests/dashboard.basic.spec.ts` — dashboard basics
- `tests/ws_envelope_e2e.spec.ts` — WS contract sanity

## Heavier / noisier suites

- `tests/visual*.spec.ts` — screenshot-based checks
- `tests/performance*.spec.ts` and `tests/chat_performance_eval.spec.ts` — perf/profiling harness
- `tests/*concurrency*.spec.ts` — parallelism/concurrency scenarios

## Architecture notes

- Playwright starts an isolated backend + frontend (see `apps/zerg/e2e/playwright.config.js`).
- The backend uses per-worker SQLite DB routing for test isolation (header-based).
- Use `cd apps/zerg/e2e && bunx playwright show-report` for failures and traces.
