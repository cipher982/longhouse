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
