# E2E Test Stabilization Spec

**Status:** In Progress
**Created:** 2026-01-06
**Protocol:** SDP-1

## Executive Summary

E2E tests are failing at a high rate (174/342 failed) due to infrastructure mismatches and selector drift, not fundamental code issues. The DB connection fixes (engine leak, SSE session lifetime) have been applied but not yet committed. This spec covers stabilizing E2E tests and committing all pending fixes.

## Current State

- **Test Results (baseline before fixes):** 57 passed, 237 failed
- **Test Results (after DB fixes):** 87-109 passed (varies due to flakiness), ~174 failed
- **Unit Tests:** All passing (backend 1310, frontend 88) when run with `PYTEST_XDIST_WORKERS=1`
- **Known Issue:** Default `make test` with `-n auto` + Testcontainers causes xdist worker crashes (pre-existing)
- **Uncommitted Changes:** ~~6 files with DB/session fixes ready to commit~~ COMMITTED (addf9f6)

## Problem Categories

### 1. Hardcoded Ports (21 occurrences)
Tests hardcode old port numbers instead of using fixtures:
- `localhost:47200` / `localhost:47300` (old dev ports)
- `localhost:8004` (unknown origin)

**Affected files:**
- `core_agent_workflow.spec.ts` - uses 8004
- `chat_sent_at_field.spec.ts` - uses 47200/47300
- `visual-ui-comparison.spec.ts` - uses 47200
- `smoke-test.spec.ts` - uses 47200/47300
- `styling-validation.spec.ts` - uses 47200

**Solution:** Replace with fixture-provided `baseURL` or `backendUrl`

### 2. Missing/Mismatched Test IDs
Tests expect testids that don't exist in the current UI:
- `global-canvas-tab` - doesn't exist anywhere
- `agent-debug-modal` - tests expect it but it may have changed

**Solution:** Either add testids to UI or update tests to match current UI

### 3. Test Helper Misuse
`safeClick()` in `test-utils.ts` expects a string selector but some tests pass Locator objects:
```typescript
// Wrong - passes Locator
await safeClick(page, page.locator('[data-testid="foo"]'));

// Correct - passes string
await safeClick(page, '[data-testid="foo"]');
```

**Affected files:**
- `agent_settings_autosave.spec.ts:52`

### 4. Backend 500 Error
`/api/admin/debug/db-schema` returns 500 when called with `X-Test-Worker: guardrail_a`:
- Endpoint: `routers/admin.py:629`
- Test: `worker_isolation_guardrail.spec.ts:46`

**Needs investigation** - likely schema doesn't exist for non-numeric worker IDs

### 5. E2E Teardown Cleanup Failure
`test-teardown.js` sometimes exits non-zero during `drop_all_e2e_schemas()`. Likely lock/connection timing issue.

## Decision Log

### Decision: Update tests to use fixtures (not add more testids)
**Context:** Two approaches possible - update tests or add testids to UI
**Choice:** Update tests to use existing testids and fixture-provided URLs
**Rationale:**
- Fixtures already exist and work correctly
- Many test failures are simply not using fixtures
- Adding testids to UI is more invasive and requires frontend changes
**Revisit if:** Multiple tests legitimately need testids that don't exist

### Decision: Commit DB fixes first, then stabilize tests
**Context:** Uncommitted DB fixes could conflict with test changes
**Choice:** Commit DB fixes as Phase 1, then proceed with test fixes
**Rationale:** DB fixes are already tested and improve test reliability
**Revisit if:** N/A

### Decision: Fix worst port issues first, defer localhost:8001 cleanup
**Context:** After fixing 47200/47300/8004 (wrong ports), Codex review found 23+ more files using hardcoded localhost:8001
**Choice:** Defer localhost:8001 cleanup; 8001 is at least the correct default E2E port
**Rationale:**
- 8004/47200/47300 were wrong ports causing connection refused errors
- 8001 is the default BACKEND_PORT, so tests work even if hardcoded
- Selector/testid issues (Phase 3) likely have higher impact on pass rate
- Can batch-fix remaining hardcoded ports later as tech debt
**Revisit if:** Tests start failing due to port mismatches

## Implementation Phases

### Phase 1: Commit Pending DB Fixes
**Goal:** Get DB/session fixes into git before further changes

**Changes to commit:**
- `apps/zerg/backend/zerg/database.py` - engine leak fix
- `apps/zerg/backend/zerg/routers/admin.py` - reset timeout fix
- `apps/zerg/backend/zerg/routers/jarvis_chat.py` - SSE session fix
- `apps/zerg/backend/zerg/routers/jarvis_runs.py` - SSE session fix
- `apps/zerg/backend/zerg/routers/jarvis_supervisor.py` - SSE session fix
- `apps/zerg/backend/zerg/routers/stream.py` - SSE session fix

**Acceptance criteria:**
- [x] All 6 files committed with descriptive message (addf9f6)
- [x] Unit tests pass (with PYTEST_XDIST_WORKERS=1; xdist instability is pre-existing)
- [x] No regressions in E2E pass count (improved from 57 baseline to 87-109)

### Phase 2: Fix Test Infrastructure
**Goal:** Update tests to use fixtures and fix helper misuse

**Status:** ✅ COMPLETED

**Tasks:**
1. ✅ Update all hardcoded port references to use `baseURL` (from config) or `backendUrl` (from fixtures)
2. ✅ Fix `safeClick()` callsites passing Locator instead of string
3. ✅ Update tests to import from `./fixtures` instead of `@playwright/test`

**Affected files (ports):**
- `core_agent_workflow.spec.ts` - Fixed 1 occurrence (unknown port 8004)
- `chat_sent_at_field.spec.ts` - Fixed 8 occurrences
- `visual-ui-comparison.spec.ts` - Fixed 3 occurrences
- `smoke-test.spec.ts` - Fixed 6 occurrences
- `styling-validation.spec.ts` - Fixed 4 occurrences

**Other fixes:**
- `agent_settings_autosave.spec.ts` - Fixed `safeClick()` misuse (passing Locator instead of string)
- `worker_isolation_guardrail.spec.ts` - Updated to import from `./fixtures`

**Acceptance criteria:**
- [x] Zero hardcoded wrong-port references (8004/47200/47300) in test files (verified with grep)
- [x] All `safeClick()` calls use string selectors (1 occurrence fixed)
- [x] E2E pass count stabilized at 90 passed (up from 57 baseline)
- [ ] **Deferred:** 23+ files still have hardcoded localhost:8001 (see Decision Log)

**Test results:** 90 passed, 186 failed, 58 skipped, 8 flaky
- Remaining failures are selector/testid issues (Phase 3 work)
- Infrastructure fixes (ports, fixtures) successfully applied

**Commits:**
- 59a5b56 - phase 2: replace hardcoded ports with fixture-provided URLs
- cb86a01 - phase 2: fix safeClick() helper misuse in agent_settings_autosave.spec.ts
- cef3f5b - phase 2: update worker_isolation_guardrail.spec.ts to import from fixtures

### Phase 3: Selector/TestID Alignment
**Goal:** Update tests expecting non-existent testids

**Tasks:**
1. Identify all tests waiting for missing testids
2. Either update tests to use existing selectors OR add minimal testids to UI
3. Focus on high-impact fixes (tests that block many others)

**Known missing testids:**
- `global-canvas-tab` - needs investigation
- `agent-debug-modal` - needs investigation

**Acceptance criteria:**
- [ ] No tests failing due to "element not found" for testid selectors
- [ ] Any new testids documented in this spec

### Phase 4: Backend Issues & Cleanup
**Goal:** Fix remaining backend issues and clean up

**Tasks:**
1. Investigate `/api/admin/debug/db-schema` 500 error with non-numeric worker IDs
2. Fix or harden `test-teardown.js` cleanup
3. Update E2E README to reflect Postgres schema isolation (not SQLite)

**Acceptance criteria:**
- [ ] `worker_isolation_guardrail.spec.ts` passes
- [ ] Teardown completes without error
- [ ] README accurate

## Test Commands

```bash
# Run all E2E tests
make test-e2e

# Run single test file
make test-e2e-single TEST=tests/chat.spec.ts

# Check errors
cat apps/zerg/e2e/test-results/errors.txt

# Query results
jq '.counts' apps/zerg/e2e/test-results/summary.json
```

## Success Criteria

- E2E pass rate > 80% (currently ~32%)
- No DB connection/pool errors in test output
- All uncommitted changes committed and pushed
- README and docs accurate
