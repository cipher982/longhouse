# Managed-Local Turn Ledger Phase 2

Status: In progress
Spec: `docs/specs/managed-local-turn-ledger.md`
Owner: Codex
Last updated: 2026-03-26

## Goal

Promote the phase-1 shadow ledger into a read path for managed-local
continuation so `/api/sessions/{id}/chat` can read terminal and durability state
from a first-class per-turn record instead of reconstructing everything from
runtime polling plus persisted events.

## Done when

- managed-local continuation can read control completion from the turn ledger
- `sync_status` can read durability from the turn ledger
- focused route/ledger tests cover ledger-terminal and ledger-durable fallback cases
- the route still passes hosted managed-local Claude stress end to end on
  `david010`

## Notes

- Phase 1 is already shipped and archived in
  `docs/tasks/done/2026-03/managed-local-turn-ledger.md`.
- Do not start this slice by adding more status enums or provider-general
  abstractions.
- Keep the first implementation narrow: managed-local continuation only.
- First pass should prefer the ledger for terminal + durability, but keep a
  bounded fallback to current direct evidence if a shadow write is missing or
  late. Do not re-open the green hosted path just to make phase 2 more pure.
- Current branch work:
  - added a committed-read snapshot helper for `managed_local_turns`
  - route now re-reads the ledger before final `done` decision and can hydrate
    durable events from the ledger baseline when the direct event waiter returns empty
  - route now prefers ledger terminal phase for `control_status` when present
  - follow-up hardening from review:
    - ledger fallback now hydrates only the exact durable event span already bound in the ledger
    - ledger reads are best-effort so DB timeout/read errors fall back to the pre-phase-2 direct evidence path
- Local verification on the current branch state:
  - targeted slice: `45 passed`
  - full backend suite: `make test` → `1174 passed`
