# Zerg 0-1 Review Remediation Plan (2026-01-19)

## Scope
Follow-up fixes from the full review. For each item:
1) Write a failing unit test to confirm the issue.
2) Fix the issue.
3) Update this doc with the decision + status.
4) Repeat.

## Priority Order (initial)
1) Thread ownership enforcement for list/update/delete (multi-tenant data leak).
2) Internal message leakage from `/threads/{id}/messages`.
3) Thread update ignores `thread_type` (silent no-op).
4) Reduce thread list payloads (messages eager-loaded in list).
5) Tool search cache `allow_pickle=True` risk.
6) Jarvis runs N+1 query.

## Decisions
- AuthZ for threads will follow existing patterns: admin can access all, non-admin only their own agents/threads.
- Internal orchestration messages should never be returned by `/threads/{id}/messages`.
- Update tests first, run through `make test` (or `make test MINIMAL=1`) to confirm failures.

## Progress Log
- 2026-01-19: Created plan; no fixes yet.

## Item 1: Thread ownership for list/update/delete
- Test: Added coverage in `apps/zerg/backend/tests/test_thread_ownership.py` for list/update/delete ownership.
- Fix: Scoped `/api/threads` list by owner (admin sees all). Added owner checks to update/delete.
- Status: DONE (make test MINIMAL=1).

## Item 2: Internal message leakage from /threads/{id}/messages
- Test: Added `test_read_thread_messages_excludes_internal` in `apps/zerg/backend/tests/test_threads.py`.
- Fix: Added `include_internal` flag to `crud.get_thread_messages` and set `include_internal=False` in the threads API.
- Status: DONE (make test MINIMAL=1).

## Item 3: Thread update ignores thread_type
- Test: Extended `test_update_thread` in `apps/zerg/backend/tests/test_threads.py` to assert thread_type updates.
- Fix: Added `thread_type` handling in `crud.update_thread` and passed through in the threads API.
- Status: DONE (make test MINIMAL=1).

## Item 4: Reduce thread list payloads
- Test: Added `test_read_threads_excludes_messages` in `apps/zerg/backend/tests/test_threads.py`.
- Fix: Added `ThreadSummary` schema for list responses, updated `/api/threads` to use it, and made `crud.get_threads` opt-in for eager messages.
- Status: DONE (make test MINIMAL=1).

## Item 5: Tool search cache allow_pickle risk
- Test: Added `test_tool_search_cache_disallows_pickle` in `apps/zerg/backend/tests/unit/test_tool_search_cache.py`.
- Fix: Switched embeddings cache load to `allow_pickle=False`.
- Status: DONE (make test MINIMAL=1).

## Item 6: Jarvis runs N+1 agent lookup
- Test: Added `TestListJarvisRuns.test_list_runs_avoids_agent_n_plus_one` in `apps/zerg/backend/tests/test_jarvis_runs.py`.
- Fix: Prefetched agents via `selectinload` and removed per-run `crud.get_agent` calls.
- Status: DONE (make test MINIMAL=1).

## Item 7: Core ThreadService ownership
- Test: Added core service tests in `apps/zerg/backend/tests/unit/test_core_thread_service.py` for list scoping + create ownership.
- Fix: Enforced ownership checks in `zerg.core.services.ThreadService` and mapped PermissionError to 403 in core router.
- Status: DONE (make test MINIMAL=1).

## Item 8: Agent idempotency cache TTL
- Test: Added `test_idempotency_cache_expires` in `apps/zerg/backend/tests/unit/test_agent_idempotency_cache.py`.
- Fix: Added TTL + timestamped entries to agent idempotency cache and expiry on lookup.
- Status: DONE (make test MINIMAL=1).

## Item 9: Doc status clarity for email connector PRD
- Update: Added explicit status note at top of `docs/completed/email_connector_prd.md` to reflect remaining TODOs.
- Status: DONE.

## Item 10: Core ThreadService O(n) thread fetch
- Issue: `ThreadService.get_threads()` iterated over each agent and fetched threads individually (O(n) queries).
- Fix: Added `owner_id` parameter to `Database.get_threads()` interface and implementations. Now uses single query with join.
- Status: DONE (make test MINIMAL=1).

## Item 11: Idempotency cache size limit
- Issue: Cache had TTL but no size limit, could grow unbounded with many unique keys.
- Fix: Added `IDEMPOTENCY_MAX_SIZE=1000` and `_cleanup_idempotency_cache()` that removes expired entries and evicts oldest when at capacity.
- Test: Added `test_idempotency_cache_enforces_size_limit`.
- Status: DONE (make test MINIMAL=1).

## Item 12: Core router clarification
- Issue: `zerg/core/routers.py` appears unused in production (not mounted in main.py).
- Analysis: It IS used by `test_main.py` for E2E test isolation via `create_app()` from factory.
- Decision: Keep as-is. The core DI architecture provides test isolation; removing it would break E2E tests.
- Status: DOCUMENTED (no code change needed).
