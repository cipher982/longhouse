# Shipper Phase 2B/2C Fixes

**Status:** In Progress
**Created:** 2026-01-28
**Protocol:** SDP-1

## Executive Summary

Post-implementation fixes for Shipper Phase 2B (real-time watching) and 2C (offline resilience). Three correctness issues identified during review:

1. Spool entries never transition to "failed" status
2. Spool/state paths ignore `CLAUDE_CONFIG_DIR`
3. Auth errors (401/403) incorrectly get spooled

## Decision Log

### Decision: Mark failed after max retries, not immediately
**Context:** `mark_failed()` needs to set status='failed' but when?
**Choice:** Set status='failed' only when retry_count >= max_retries (default 5)
**Rationale:** Transient errors should retry; permanent failures should stop
**Revisit if:** Users report items stuck in retry limbo

### Decision: Pass claude_config_dir through ShipperConfig
**Context:** Spool/state need same base path as shipper
**Choice:** SessionShipper passes its config to OfflineSpool/ShipperState constructors
**Rationale:** Single source of truth for config, no magic defaults
**Revisit if:** Users need independent spool/state locations

### Decision: Hard-fail on 401/403, spool on 5xx/timeouts
**Context:** Which HTTP errors should spool vs hard-fail?
**Choice:** Auth errors (401, 403) raise immediately; server errors (5xx) and timeouts spool
**Rationale:** Auth won't fix itself; server issues are transient
**Revisit if:** Need more granular error classification

---

## Implementation Phases

### Phase 1: Fix spool status transitions
**Goal:** Ensure failed items eventually get status='failed'

**Changes:**
- `spool.py`: `mark_failed()` sets status='failed' when retry_count >= threshold
- Add `max_retries` parameter to control threshold

**Acceptance Criteria:**
- [ ] Items with retry_count >= max_retries have status='failed'
- [ ] Failed items don't appear in `dequeue_batch()`
- [ ] `pending_count()` decreases as items fail out
- [ ] Tests updated and passing

**Test command:** `uv run pytest tests/services/shipper/test_spool.py -v`

---

### Phase 2: Unify config paths
**Goal:** Spool and state use same base path as shipper

**Changes:**
- `state.py`: Accept optional `base_path` parameter, default to claude_config_dir
- `spool.py`: Accept optional `base_path` parameter, default to claude_config_dir
- `shipper.py`: Pass `config.claude_config_dir` when creating state/spool

**Acceptance Criteria:**
- [ ] With `CLAUDE_CONFIG_DIR=/custom`, spool creates at `/custom/zerg-shipper-spool.db`
- [ ] With `CLAUDE_CONFIG_DIR=/custom`, state creates at `/custom/zerg-shipper-state.json`
- [ ] Tests pass with custom paths
- [ ] Existing tests still pass (default behavior unchanged)

**Test command:** `uv run pytest tests/services/shipper/ -v`

---

### Phase 3: Auth error handling
**Goal:** Don't spool auth errors (401/403)

**Changes:**
- `shipper.py`: In `ship_session()`, catch `HTTPStatusError` and check status code
  - 401/403: Re-raise (hard fail)
  - 5xx: Spool for retry
  - 4xx (other): Log and skip (bad payload won't fix itself)

**Acceptance Criteria:**
- [ ] 401/403 errors propagate up (not spooled)
- [ ] 500/502/503 errors get spooled
- [ ] 400/404 errors logged but not spooled
- [ ] Tests cover auth error scenario

**Test command:** `uv run pytest tests/services/shipper/test_spool.py -v`

---

### Phase 4: Final verification
**Goal:** Confirm all shipper tests pass, no regressions

**Acceptance Criteria:**
- [ ] `uv run pytest tests/services/shipper/ -v` all pass
- [ ] `make test` passes (excluding known unrelated failures)

---

## Test Commands

```bash
# Single module
uv run pytest tests/services/shipper/test_spool.py -v

# All shipper tests
uv run pytest tests/services/shipper/ -v

# Full suite
make test
```

---

## Files Modified

| File | Phase | Change |
|------|-------|--------|
| `services/shipper/spool.py` | 1, 2 | Status transitions, base_path param |
| `services/shipper/state.py` | 2 | base_path param |
| `services/shipper/shipper.py` | 2, 3 | Pass config to deps, auth error handling |
| `tests/services/shipper/test_spool.py` | 1, 3 | New tests for status, auth errors |
