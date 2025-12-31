# Beta Readiness Fixes Spec

**Status:** In Progress
**Created:** 2025-12-30
**Protocol:** SDP-1

## Executive Summary

Live eval tests revealed 3 failing behavior tests that need fixes before beta. Investigation found clear root causes with straightforward fixes.

## Decision Log

### Decision: Always drain workers in-process during evals
**Context:** Live mode was skipping worker draining, causing timeouts
**Choice:** Remove the `if eval_mode != "live"` conditional - always drain workers in evals
**Rationale:** Evals need deterministic worker completion regardless of mode. Live mode tests the LLM, not the background processor.
**Revisit if:** We need to test actual background worker processing

### Decision: Auto-seed servers to knowledge base
**Context:** knowledge_search returns empty because servers aren't in knowledge DB
**Choice:** Add server info as a KnowledgeDocument during auto_seed startup
**Rationale:** Follows existing auto-seed patterns, makes knowledge_search immediately useful
**Revisit if:** We add a dedicated get_my_servers tool

## Issues to Fix

| # | Issue | Severity | Root Cause | Fix |
|---|-------|----------|------------|-----|
| 1 | Worker delegation timeouts in live evals | HIGH | Live mode skips `_process_queued_worker_jobs()` | Remove conditional |
| 2 | knowledge_search returns empty for servers | MEDIUM | Server info in prompts, not knowledge DB | Auto-seed servers |
| 3 | Prod API health check URL confusion | LOW | Docs say `api.swarmlet.com` but it's `swarmlet.com/api/` | Update docs |

## Implementation Phases

### Phase 1: Fix Live Mode Worker Draining (Easy Win)

**Scope:** One-line fix in eval runner

**File:** `apps/zerg/backend/evals/runner.py`

**Change:**
```python
# Line ~157 - Remove the conditional, always drain workers
# FROM:
if eval_mode != "live":
    await self._process_queued_worker_jobs(supervisor_run_id=result.run_id)

# TO:
# Always drain workers in-process during evals (live uses real LLM but still drains synchronously)
await self._process_queued_worker_jobs(supervisor_run_id=result.run_id)
```

**Acceptance Criteria:**
- [ ] Conditional removed from runner.py
- [ ] Hermetic tests still pass (53/53)
- [ ] Live delegation tests no longer timeout

**Test Command:** `make eval`

### Phase 2: Auto-seed Servers to Knowledge Base

**Scope:** Add server info to knowledge DB during startup

**File:** `apps/zerg/backend/zerg/services/auto_seed.py`

**Changes:**
1. Add function `_seed_server_knowledge()` that:
   - Gets user context servers
   - Creates a KnowledgeSource named "User Context - Servers"
   - Creates KnowledgeDocument with formatted server info
   - Uses upsert pattern (idempotent)

2. Call it from `seed_user_context()` after loading context

**Acceptance Criteria:**
- [ ] New seeding function added
- [ ] Server info searchable via knowledge_search
- [ ] Idempotent (safe to run multiple times)
- [ ] Hermetic tests still pass

**Test Command:** `make eval` + manual test of knowledge_search

### Phase 3: Verify with Live Tests

**Scope:** Run full live test suite to confirm fixes

**Acceptance Criteria:**
- [ ] `make eval-live` passes 8/9 tests (edge_unknown_capability may still timeout)
- [ ] delegation_infrastructure: PASS
- [ ] delegation_task_clarity: PASS
- [ ] tool_knowledge_base: PASS

**Test Command:** `EVAL_MODE=live make eval-live`

## Files to Modify

| File | Changes |
|------|---------|
| `apps/zerg/backend/evals/runner.py` | Remove live mode conditional for worker draining |
| `apps/zerg/backend/zerg/services/auto_seed.py` | Add server knowledge seeding |

## Out of Scope

- Production API URL documentation (cosmetic, not blocking beta)
- Background worker processor in evals (not needed with synchronous draining)
- Dedicated get_my_servers tool (knowledge_search with seeding is sufficient)
