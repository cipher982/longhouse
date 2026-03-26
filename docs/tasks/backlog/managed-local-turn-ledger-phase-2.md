# Managed-Local Turn Ledger Phase 2

Status: Backlog
Spec: `docs/specs/managed-local-turn-ledger.md`
Owner: Unassigned
Last updated: 2026-03-26

## Goal

Promote the phase-1 shadow ledger into a read path for managed-local
continuation so `/api/sessions/{id}/chat` can read terminal and durability state
from a first-class per-turn record instead of reconstructing everything from
runtime polling plus persisted events.

## Done when

- managed-local continuation can read control completion from the turn ledger
- `sync_status` can read durability from the turn ledger
- the route still passes hosted managed-local Claude stress end to end on
  `david010`

## Notes

- Phase 1 is already shipped and archived in
  `docs/tasks/done/2026-03/managed-local-turn-ledger.md`.
- Do not start this slice by adding more status enums or provider-general
  abstractions.
- Keep the first implementation narrow: managed-local continuation only.
