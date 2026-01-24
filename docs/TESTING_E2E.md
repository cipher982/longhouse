# banana — E2E Handoff (make it fast, parallel, trustworthy)

This is the handoff doc for getting Zerg’s E2E suite back to the intended end state:

- Tests run **often** (agent-written code needs a hard gate)
- Tests run **fast** (parallel)
- Failures are **real** (no “green by skipping everything”)

## The Principles (first principles)

1) **Parallelism should reveal bugs, not create them.**
   - If parallel makes tests “flaky”, that usually means a shared-state leak or a missing readiness wait.
2) **Correctness first, then speed.**
   - If the suite can’t be trusted, it doesn’t matter how fast it runs.
3) **Avoid “silent success”.**
   - Any reset/seed helper must either succeed or fail loudly.
4) **Never sleep when you can wait for a condition.**
   - `waitForTimeout()` is a last resort; prefer `waitForResponse`, `expect(...).toBeVisible`, `expect.poll`, etc.

## Current Architecture (what’s actually happening)

### Isolation model
- Each Playwright worker has its own Postgres schema: `e2e_worker_0`, `e2e_worker_1`, …
- Routing to schemas is done by request header: `X-Test-Worker: <id>`
- E2E requests should go through:
  - Playwright `request` fixture (preferred), or
  - Helpers that *correctly propagate* worker id (never hardcode `"0"`).

### Why `uvicorn` workers matter
Playwright workers spam the API in bursts. If the backend only has 1 process, UI tests time out waiting on queued responses even if the app is “correct”.

This repo currently pins sensible defaults for reproducibility and tunes DB pool sizes to avoid connection explosions:
- Playwright workers: `PLAYWRIGHT_WORKERS`
- Uvicorn workers: `UVICORN_WORKERS`
- DB pool safety (E2E): `E2E_DB_POOL_SIZE`, `E2E_DB_MAX_OVERFLOW`

## Two Chat UIs (don’t mix selectors)

**Agent Chat (dashboard-driven)**
- Uses `data-testid`:
  - `[data-testid="chat-input"]`
  - `[data-testid="send-message-btn"]`
  - `[data-testid="messages-container"]`

**Jarvis Chat**
- Route: `/chat`
- Uses class selectors (today):
  - `.text-input`
  - `.send-button`
  - `.message.user` / `.message.assistant`

If you use the wrong selector set, it looks like flake but it’s just wrong.

## What counts as “done” (end state)

1) No unconditional file-level `test.skip()` in core suites.
2) The suite passes in CI and locally under the chosen concurrency defaults.
3) Any “known broken” test is either:
   - Fixed, or
   - Explicitly quarantined with a clear reason + ticket/reference, or
   - Deleted only if the feature no longer exists.

## The Workflow to Re-enable Tests (no thrash)

Per file:
1) Remove file-level skip.
2) Run a single spec.
3) Fix it to green under parallel load.
4) Commit.

Commands (always use make targets):
- `make test-e2e-single TEST=<spec>`
- `make test-e2e-grep GREP="<test name>"`
- `make test-e2e-errors`
- `make logs`

## How to Fix 80% of Failures Quickly

### 1) Selector drift → add stable `data-testid`
- Prefer updating the frontend to expose stable testids over brittle CSS chains.
- Keep them scoped and intentional (don’t turn the app into a “test DOM”).

### 2) Readiness waits → wait for the right condition
Common fixes:
- After clicking a submit button, wait for the **network** response that persists data, not the optimistic UI update.
- When a panel is “hidden”, decide whether it should be **not rendered** (`toHaveCount(0)`) or **rendered-but-hidden** (`not.toBeVisible()`), and assert the correct one.

### 3) Shared state → isolate or make test data unique
- If a test depends on “no agents exist”, that’s a smell unless it truly runs in an isolated schema and the reset actually ran.

## Speed Roadmap (get to “no per-test reset”)

The long-term fast setup is:
- GlobalSetup: create `e2e_worker_*` schemas once (empty tables)
- Each worker uses its schema all run
- No per-test reset (tests create what they need)
- Rarely: allow a `truncateWorkerSchema()` helper for tests that truly require emptiness

This removes minutes of wall time from large suites and avoids DDL/catalog lock contention.

## Guardrails (prevent backsliding)

Strong recommendation:
- Don’t accept PRs that make CI green by skipping large swaths of tests.
- If you must quarantine, make it temporary and loud:
  - add a reason string
  - track the count of quarantined tests and burn it down

### Step 3 — Make selectors stable (add testids, don’t chase CSS)

If you want “AI writes 90% of code + tests always catch regressions”, you need stable selectors.

Rule:
- Prefer `data-testid` over classes/text.
- If a test currently uses `.some-class`, add a `data-testid` in the React component and update the test.

This is the single biggest long-term leverage point for “robustly test everything”.

### Step 4 — Reduce reliance on per-test resets (performance + stability)

Long-term best practice (fastest):
- Schema-per-worker created once in `globalSetup`
- Tests create their own data and don’t assume empty state
- Only the rare test uses a “truncate worker schema” helper

Short-term pragmatic:
- Keep per-test `reset-database` for now
- But start migrating “happy path” tests to be independent of empty DB

## Quarantine policy (if you absolutely must keep CI green temporarily)

If you keep a quarantine phase, make it visible and painful:
- CI step prints:
  - number of skipped tests
  - list of skipped files
- Hard cap that goes down each PR (e.g., “skips must decrease by ≥5 each PR”).

Do **not** allow indefinite “233 skipped” to become normal.

## How to debug fast (no manual clicking)

Use the repo’s existing tooling:
- Run a single spec:
  - `make test-e2e-single TEST=tests/<spec>.spec.ts`
- On failure:
  - `make test-e2e-errors`
  - `make test-e2e-query Q='.failed[] | {file, title, line}'`
- Artifacts:
  - `apps/zerg/e2e/test-results/` (screenshots, traces, errors)

For test authorship:
- Avoid `waitForTimeout` unless you’re explicitly testing time-based behavior.
- Prefer `await expect(locator).toBeVisible({ timeout: ... })` and `await expect.poll(...)`.

## Known Buckets (what was skipped/deleted and why)

From `bb9b506` message:
- Canvas: selectors changed (`#agent-shelf`, `.agent-pill` assumptions)
- Chat: new structure (agent vs Jarvis confusion)
- WebSocket subscription: missing ack behavior
- Agent settings: UI refactor (“Allowed Tools” → “Integrations”)
- Perf/load/concurrency: expensive + flake-prone (but these should exist eventually)

Treat these as a prioritized backlog, not “delete and forget”.

## Recommended next PR (concrete)

1) Pick 1–3 quarantined “core” specs and fully re-enable them to green.
2) Add missing `data-testid`s for the selectors that drifted (small UI change, huge test leverage).
3) Keep worker isolation invariant:
   - no hardcoded localhost URLs
   - no API clients with worker `"0"` unless single-worker by design

If you do that consistently, you’ll climb from “66 passed, 233 skipped” back toward “300+ passed, 0 skipped” without the flapping.
