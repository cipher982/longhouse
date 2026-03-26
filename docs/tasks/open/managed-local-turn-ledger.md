# Managed-Local Turn Ledger

Status: In progress
Spec: `docs/specs/managed-local-turn-ledger.md`
Owner: Codex
Last updated: 2026-03-25

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

- [ ] Add formal spec and active task tracking
- [ ] Add minimal `managed_local_turns` model + service helpers
- [ ] Shadow ledger creation + send acceptance in session continuation
- [ ] Shadow terminal update from the existing managed-local route path
- [ ] Shadow durability binding from ingest
- [ ] Shadow review attachment from turn review creation
- [ ] Add targeted tests
- [ ] Ship and verify hosted managed-local continuation on `david010`

## Notes

- Keep this slice additive. Do not change `/api/sessions/{id}/chat` semantics in phase 1.
- Prefer timestamps + bound ids over extra status enums.
- Treat one outstanding managed-local continuation per session thread as the
  expected path; do not overbuild concurrent turn matching yet.
