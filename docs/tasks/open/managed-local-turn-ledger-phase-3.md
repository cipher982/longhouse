# Managed-Local Turn Ledger Phase 3

Status: In progress
Spec: `docs/specs/managed-local-turn-ledger.md`
Owner: Codex
Last updated: 2026-03-26

## Goal

Make managed-local `turn_loop` consume durable ledger rows directly instead of
reconstructing completed turns from transcript scans plus session-wide presence
inference.

## Done when

- `turn_loop` can identify the next managed-local turn to review from the ledger
- the matching `SessionTurnReview` attaches back to that turn row
- the hosted managed-local Claude path still passes end to end on `david010`

## Notes

- Phase 1 shadow ledger is archived in
  `docs/tasks/done/2026-03/managed-local-turn-ledger.md`.
- Phase 2 route read-path is archived in
  `docs/tasks/done/2026-03/managed-local-turn-ledger-phase-2.md`.
- Keep the first phase-3 slice narrow:
  - managed-local sessions only
  - consume oldest durable ledger row with no `review_id`
  - do not rewrite all transcript/review logic at once
- Current branch work:
  - added a managed-local ledger selector for the next durable, unreviewed turn
  - `turn_loop_retry_needed()` and `_record_session_turn_review()` now prefer that ledger-selected assistant event for managed-local sessions
  - added a regression proving review order follows durable ledger order instead of always picking the latest transcript assistant turn
  - older targeted turns now reconstruct dialog from history up to the specific assistant event id, not from the live tail
- Local verification on the current branch state:
  - targeted slice: `28 passed`
  - full backend suite: `make test` → `1176 passed`
