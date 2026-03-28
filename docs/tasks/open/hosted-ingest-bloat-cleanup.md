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
- [x] Apply the minimum safe prod mitigation for `david010`
- [x] Patch the ingest/runtime code to block recurrence
- [x] Add focused regression coverage
- [x] Verify tenant health improves after mitigation

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

## Follow-up Fix (2026-03-28)

- The first engine fix was incomplete for real Codex child transcripts.
  - Literal child files can contain two top-level `session_meta` lines:
    1. child session id + `forked_from_id`
    2. injected parent session context
  - The parser was letting the later parent `session_meta.id` override the earlier child id, so explicit child re-ships still collapsed into the parent even after the initial fix.
- Landed parser follow-up:
  - first valid Codex `session_meta.id` now wins for the file
  - regression test covers the exact child-then-parent `session_meta` shape from the live bad file
- Commit: `bc3d3803` (`Fix Codex child session_meta override order`)
- Local remediation:
  - rebuilt + reinstalled `longhouse-engine` on this laptop with `make install-engine`
  - pushed the parser follow-up to `main`

## Prod Mitigation (2026-03-28)

- Stopped `longhouse-david010` and repaired `/var/app-data/longhouse/david010/longhouse.db` offline.
- Removed all false truncation descendants for session `019d1805-66b6-78f1-aca9-91225867663d`.
  - deleted `12` descendant branches
  - deleted `1,114,740` descendant-branch events
  - deleted `1,999,445` descendant-branch source lines
- Removed duplicate child transcripts from the parent root branch where those child sessions already existed separately.
  - first pass removed `42` duplicate child source paths:
    - `97,440` root-branch events
    - `175,158` root-branch source lines
  - second pass removed one remaining separately-present child path that was missed by the first offset-0-only classification:
    - `2,423` root-branch events
    - `4,353` root-branch source lines
- Cleared derived rows tied to the corrupted parent session:
  - deleted `84` `session_turn_reviews`
  - deleted `539` `session_embeddings`
- Reset the parent session head to the root branch and recomputed counts from the remaining root events.
- Checkpointed the WAL to zero and restarted the tenant.
- Recycled host swap after the tenant stabilized.

## Re-Pollution Follow-up (2026-03-28 evening)

- The first offline repair held only briefly because the local launch agent on this laptop was still running an older `longhouse-engine connect` binary.
  - Engine log `~/.claude/logs/engine.log.2026-03-28` shows the daemon replaying the same March 23/24 Codex files again around `2026-03-28T19:34Z` through `19:43Z`.
  - That replay recreated two rewrite descendant branches on the parent session:
    - branch `13023` (`rewrite` off the parent root path at offset `9072675`)
    - branch `13030` (`rewrite` off child path `019d1d6c-...` at offset `9087650`)
- Verified the hosted tenant runtime itself was already on the fixed image:
  - `longhouse-david010` image revision label: `78c317fa44d6de5dd814eba2e0cf55599c1835bd`
- Prevented further churn before the second repair:
  - unloaded local launch agent `com.longhouse.shipper`
  - rebuilt and reinstalled the current engine with `make install-engine`
  - confirmed the local default spool had `0` pending entries for the March 23/24 problem files before bringing the daemon back

## Backfill + Final Repair (2026-03-28 evening)

- Re-shipped the `3` previously-missing child Codex transcripts with the repo-local fixed engine and an isolated temp shipper DB (`/tmp/lh-codex-backfill-20260328.db`), so the repair did not depend on stale `~/.claude/longhouse-shipper.db` state.
  - `019d1bb1-15c1-78c0-b4bc-f830965f237b` → `1034` events shipped
  - `019d1c56-bd09-7062-90a8-d3f765689054` → `1932` events shipped
  - `019d1d6c-a78d-70b2-875f-d9c500256c54` → `3554` events shipped
- Verified those `3` child sessions now exist separately on the tenant with their own session ids / provider session ids.
- Stopped `longhouse-david010` again and repaired the tenant DB offline a second time.
  - deleted descendant rewrite branches `13023` and `13030`
  - deleted all descendant-branch events / source lines for those branches
  - deleted the final `3` embedded child source paths from the parent root branch
  - cleared parent `session_turn_reviews` and `session_embeddings` again
  - reset parent summary fields / summary cursor and marked embeddings dirty
  - checkpointed WAL and restarted the tenant
- Restarted the local launch agent afterward on the rebuilt engine.
  - the rebuilt daemon starts cleanly with `connect --log-dir ...`
  - after the final local metadata repair, startup catch-up reported `0` retry paths
- Repaired stale local shipper metadata in `~/.claude/longhouse-shipper.db`.
  - updated `27` Codex `file_state` rows that still pointed child rollout paths at the parent session id
  - all `27` child ids now match their rollout filename UUIDs locally, and all `27` also exist separately on the hosted tenant

## Current State

- Host health:
  - swap back to `0 B` used after `swapoff`/`swapon`
  - host has ~`12 GiB` available RAM
- Tenant health:
  - `https://david010.longhouse.ai/api/health` is healthy
  - write serializer is back to low queue latency (`avg_queue_wait_ms` ~`1.4 ms` after restart)
  - tenant WAL is gone after checkpoint (`longhouse.db-wal` absent)
- Corrupted parent session now:
  - single root branch only: `12335`
  - counts now `99 / 3149 / 2461` (`user / assistant / tool`)
  - root branch now has `5710` events and exactly `1` `source_path`
  - remaining root source path is only the true parent transcript:
    - `/Users/davidrose/.codex/sessions/2026/03/22/rollout-2026-03-22T21-08-20-019d1805-66b6-78f1-aca9-91225867663d.jsonl`
- Child backfills now exist separately on the tenant:
  - `019d1bb1-15c1-78c0-b4bc-f830965f237b` → `31 / 87 / 458`
  - `019d1c56-bd09-7062-90a8-d3f765689054` → `43 / 193 / 848`
  - `019d1d6c-a78d-70b2-875f-d9c500256c54` → `73 / 411 / 1535`

## Ops Follow-up

- Logical data cleanup is complete; the remaining issue is physical SQLite file bloat.
  - `longhouse.db` is still about `40 GiB` because deletes reclaimed WAL pages but did not rewrite the main DB file.
  - A deliberate offline `VACUUM` would reclaim disk if/when we want compaction; host free space is still comfortable (`143 GiB` available on `/var/app-data`), so this is not urgent for correctness or runtime stability.
