# Managed-Local Turn Ledger

Status: In progress
Spec: `docs/specs/managed-local-turn-ledger.md`
Owner: Codex
Last updated: 2026-03-26

## Goal

Lay down the smallest solid foundation for per-turn managed-local truth:
create a monotonic shadow ledger for continuation turns, wire it into the
existing path without changing user-facing behavior, and validate it end to end.

## Done when

- `managed_local_turns` exists with the minimal phase-1 shape.
- Managed-local `/api/sessions/{id}/chat` creates and updates ledger rows in shadow mode.
- Transcript ingest can mark the current managed-local turn durable.
- Turn review creation can attach the review to the matching turn row.
- Targeted tests and hosted prod verification pass on `david010`.

## Checklist

- [x] Add formal spec and active task tracking
- [x] Add minimal `managed_local_turns` model + service helpers
- [x] Shadow ledger creation + send acceptance in session continuation
- [x] Shadow terminal update from the existing managed-local route path
- [x] Shadow durability binding from ingest
- [x] Shadow review attachment from turn review creation
- [x] Add targeted tests
- [ ] Ship and verify hosted managed-local continuation on `david010`

## Notes

- Keep this slice additive. Do not change `/api/sessions/{id}/chat` semantics in phase 1.
- Prefer timestamps + bound ids over extra status enums.
- Treat one outstanding managed-local continuation per session thread as the
  expected path; do not overbuild concurrent turn matching yet.
- Phase 1 shadow ledger is implemented in code:
  - model in `server/zerg/models/agents.py`
  - service helpers in `server/zerg/services/managed_local_turns.py`
  - shadow writes from `session_chat.py`, `agents_store.py`, and `session_turn_reviews.py`
- Reviewer findings addressed before ship:
  - ledger writes are now best-effort and isolated so shadow failures do not break the live path
  - late durability can heal a `turn_timeout` row instead of leaving the ledger permanently wrong
- Local verification on the current branch state:
  - targeted slice: `41 passed`
  - full backend suite: `make test` → `1170 passed`
