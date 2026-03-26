# Managed-Local Turn Ledger Phase 3

Status: Complete
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
  - reviewer follow-up fix: durable ledger binding now tracks the last real assistant reply event for a turn, not a tool placeholder
  - reviewer follow-up fix: stale or unreconstructable managed-local ledger rows are skipped so they do not wedge newer durable turns behind them
- Local verification on the current branch state:
  - targeted slice: `35 passed`
  - full backend suite: `make test` → `1179 passed`
- Hosted verification on `david010`:
  - `make qa-live` → `11 passed`
  - `./scripts/hosted-managed-local-claude-stress.sh --subdomain david010` → `4/4 passed`
  - `./scripts/hosted-loop-debug.sh --subdomain david010 --session 9cb44e34-fd0b-437c-8d1b-7d685c98b9e6 --json`
    confirmed 4 recorded reviews with matching `managed_local_turns.review_id` attachments and no `error_code`
