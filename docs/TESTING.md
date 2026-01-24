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

## UI / Design QA (Quick Checks)

| Command | What | When |
|---------|------|------|
| `make qa-ui` | Accessibility + UX heuristics (axe + custom) | Every UI change |
| `make qa-ui-visual` | Visual analysis screenshots + AI notes | Before merging UI work |
| `make qa-ui-baseline` | Run public + app baseline screenshots | Desktop UI lock |
| `make qa-ui-baseline-update` | Update public + app baseline screenshots | After intentional UI updates |
| `make qa-ui-baseline-mobile` | Run mobile viewport baselines (iPhone 13 + iPhone SE) | Mobile layout check |
| `make qa-ui-baseline-mobile-update` | Update mobile viewport baselines (iPhone 13 + iPhone SE) | After mobile UI updates |
| `make qa-ui-full` | Full UI regression sweep (a11y + desktop + mobile baselines) | Pre-merge UI validation |
| `make test-e2e-ui` | Interactive Playwright UI runner | Debugging layout/flows |
| `make test-e2e-single TEST=tests/<spec>.ts` | Targeted flow check | Focused iteration |
| `make test-e2e-single TEST=tests/ui_baseline_public.spec.ts` | Public page visual baselines | Smoke visual diffs |
| `PWUPDATE=1 make test-e2e-single TEST=tests/ui_baseline_public.spec.ts` | Update public page baselines | When UI changes |
| `make test-e2e-single TEST=tests/ui_baseline_app.spec.ts` | App page visual baselines (dashboard/chat/canvas/settings/profile/etc) | Desktop UI lock |
| `PWUPDATE=1 make test-e2e-single TEST=tests/ui_baseline_app.spec.ts` | Update app baselines | When app UI changes |
| `make test-e2e-single TEST="--project=mobile tests/mobile/ui_baseline_mobile.spec.ts"` | Mobile baselines (iPhone 13) | Mobile lock |
| `PWUPDATE=1 make test-e2e-single TEST="--project=mobile tests/mobile/ui_baseline_mobile.spec.ts"` | Update mobile baselines (iPhone 13) | When mobile UI changes |
| `make test-e2e-single TEST="--project=mobile-small tests/mobile/ui_baseline_mobile.spec.ts"` | Mobile baselines (iPhone SE) | Small-screen check |
| `PWUPDATE=1 make test-e2e-single TEST="--project=mobile-small tests/mobile/ui_baseline_mobile.spec.ts"` | Update mobile baselines (iPhone SE) | When mobile UI changes |

**Tips**
- Run `make qa-ui-visual ARGS="--pages=dashboard,chat --headed"` to watch captures live.
- Visual analysis writes reports to `apps/zerg/e2e/visual-reports/` and screenshots in `apps/zerg/e2e/test-results/`.
- Use `PWUPDATE=1` when you intentionally change UI and want to refresh snapshot baselines.
