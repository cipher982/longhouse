# Hosted Ingest Bloat Cleanup

Status: In progress
Last updated: 2026-03-28

## Goal

Stop the live `david010` hosted tenant from thrashing on giant repeated Codex ingest, identify why one ended session kept growing after the source shipper should have been done, and land guardrails plus a repair path so one bad session cannot fill RAM, swap, WAL, and the write queue again.

## Done when

- The live replay pressure on `david010` is understood and stopped.
- We can explain whether the huge counts are true session volume, branch/rewrite inflation, or ingest corruption.
- Longhouse has a code-level guard or dedupe fix that prevents the same failure mode from recurring.
- The repair path for already-bloated sessions/tenants is documented in this task file and, where reasonable, automated.

## Checklist

- [x] Confirm how the giant session inflated: repeated full replay, branch rewrite churn, source-line duplication, or counter bug
- [x] Identify the active ingest source for the bad session
- [ ] Apply the minimum safe prod mitigation for `david010`
- [x] Patch the ingest/runtime code to block recurrence
- [x] Add focused regression coverage
- [ ] Verify tenant health improves after mitigation

## Notes

- Current live hotspot: hosted tenant `david010` on `zerg`
- Session `019d1805-66b6-78f1-aca9-91225867663d` is a `codex` session from `cinder`/`shipper-laptop`, started `2026-03-23`, ended `2026-03-24`, but was still being updated on `2026-03-28`
- Current observed size/pressure:
  - container `longhouse-david010` at ~`9.3 GiB` RSS / ~`60%` CPU
  - host swap essentially full (`2.0 GiB` used)
  - tenant DB `longhouse.db` ~`33 GiB`, WAL ~`12 GiB`
  - session row shows `1974` user messages, `9553` assistant messages, `39722` tool calls
  - events table currently has `793575` rows for that session
- Local `~/.claude/longhouse-shipper.db` on this laptop shows the original rollout file for that session fully acked with no current spool rows for that path, so the present replay pressure may be from another shipper/client or a different replay path.

## Confirmed Root Cause

- The huge parent session is genuinely corrupted, not just unusually long.
  - The original local parent rollout file `rollout-2026-03-22T21-08-20-019d1805-...jsonl` has about `10,837` raw lines, about `104` real user messages, and about `2,540` Codex tool calls.
  - The hosted tenant head branch for session `019d1805-...` had about `1,956` user messages and about `39k` tool calls, so the inflation happened after shipping/ingest.
- The parent session absorbed `46` distinct Codex child rollout files.
  - Their offset-0 `session_meta` rows have their own `payload.id` values and `forked_from_id:"019d1805-..."`.
  - Those child transcripts landed under the parent Longhouse session instead of separate child/sidechain sessions.
- The open-source Codex repo confirms the relevant behavior:
  - forked child threads write `session_meta` with a child session id plus `forked_from_id`
  - hook subprocesses inherit the Codex process environment
  - hook event payloads do not expose a dedicated “this is a subagent child” field, so the transcript header is the reliable signal
- Longhouse-side failure chain:
  1. Managed-local Codex shipping passed `--session-id "$LONGHOUSE_SESSION_ID"` into `longhouse-engine ship --file`.
  2. Forked child transcripts inherited that managed parent env var, so their ship path overrode the child ingest UUID with the parent Longhouse UUID.
  3. The parent session then accumulated many child transcript source paths and counts.
  4. Later partial replay/retry batches for those source paths hit backend rewind detection, which treated any smaller historical chunk as `truncation`.
  5. Each false truncation fork copied almost the entire prior head branch, exploding events/source_lines/WAL/RSS/swap.

## Landed Fix

- Engine/parser:
  - Codex transcript parsing now preserves `forked_from_id` on metadata and normalizes parsed event session ids to the canonical `session_meta` id.
  - Managed `--session-id` override is now ignored for forked Codex child transcripts, so subagent files no longer collapse into the managed parent session during fresh ship or spool replay.
- Backend ingest:
  - Rewind detection no longer infers `truncation` from `incoming_max_offset < existing_max_offset`.
  - We still fork on same-offset content rewrites and lineage divergence, but identical historical partial replays stay on the existing head branch.
- Regression coverage:
  - engine parser/shipper tests cover forked Codex child detection and override suppression
  - lite backend tests cover identical partial historical replay without branch churn

## Ops Follow-up

- Deploy the engine/backend fix before touching tenant cleanup, otherwise replay can immediately re-pollute the same session.
- After deploy, repair `david010`:
  - remove the false truncation descendant branches for `019d1805-...`
  - decide whether to split or delete the child source paths already merged into the parent session
  - checkpoint/truncate WAL and likely restart or reprovision `longhouse-david010`
- Re-check host memory/swap and tenant write-queue latency after cleanup.
