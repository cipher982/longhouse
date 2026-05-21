# Session Enrichment Revision-Lag Reconciliation

Status: Implemented
Owner: David Rose
Created: 2026-05-20
Reviewed: 2026-05-20 via Hatch Opus
Implemented: 2026-05-20

## Executive Summary

Longhouse treats raw session ingest as the product truth and derived session metadata as opportunistic enrichment. The removed post-ingest pipeline created durable `SessionTask` rows for summary and embedding work, then drained those rows through a cold worker. That made remote API work behave like a local single-lane queue: live summary/title work, embeddings, historical reingest, retries, and backfill all competed behind the same worker and the same SQLite write serializer.

The replacement is revision-lag reconciliation:

```text
summary work is needed when summary_revision < transcript_revision
embedding work is needed when embedding_revision < transcript_revision or needs_embedding = 1
```

The database state is the queue. Workers scan session rows, choose the highest-value stale sessions, run bounded concurrent remote API calls, and advance the relevant revision only if the write still matches the observed transcript revision. A missing summary/title never blocks the timeline: cards use stored summary/title when available and deterministic fallbacks otherwise.

`SessionTask` remains as a compatibility table, but summary and embedding enrichment moved out of it. Review also found no active production producer/executor for `turn_loop`, so Phase 4 removed dormant ingest-task worker scaffolding.

## Starting State

Useful existing primitives:

- `sessions.transcript_revision` increments when durable transcript events change.
- `sessions.summary_revision` records the transcript revision covered by the current summary/title.
- `sessions.embedding_revision` records the transcript revision covered by stored embeddings.
- `sessions.needs_embedding` marks rows needing embedding reconciliation.
- `generate_summary_impl(session_id)` already skips provider calls when `summary_revision >= transcript_revision`.
- `generate_embeddings_impl(session_id)` already skips provider calls when `embedding_revision >= transcript_revision`.
- Summary and embedding implementations close read DB sessions before remote provider calls.
- Timeline cards now have client-side first-user/title fallback and no longer display a fake "Generating summary" body.

Removed shape:

- `enqueue_ingest_tasks()` inserts `summary` and `embedding` `SessionTask` rows after every transcript-changing ingest.
- The cold worker defaults to one concurrent task and claims one task at a time.
- Summary, embedding, and historical reingest-derived work share that cold lane.
- Task claim/done/timeout writes go through `WriteSerializer`, so heavy archive ingest can delay even tiny bookkeeping writes.
- `summary_status` projection currently reads latest `SessionTask(summary)` rows, so old/pending task rows leak queue internals into API/UI semantics.
- Retry/resurrection logic exists for summary/embedding even though revision counters already describe whether the derived state is stale.
- The hot `turn_loop` worker existed structurally, but review found no production enqueue path and no `_run_task_impl` execution branch for `turn_loop`.

Observed failure:

- Hosted session `42c70f5c-9990-4051-ab69-8c5ef27113d3` had `summary_revision=0`, `transcript_revision=8`, `summary=null`, `summary_title=null`.
- It had a `summary` `SessionTask` created at `2026-05-21 01:04:56Z`, status `pending`, attempts `0`.
- Tenant backlog at investigation time included hundreds of pending summaries and over a thousand pending embeddings.
- Logs showed task status writes waiting behind ingest writes. This was queue architecture starvation, not a one-session summarizer bug.

## End State

Longhouse should satisfy these invariants:

- Raw session ingest is authoritative and durable.
- Timeline/list/search surfaces remain useful without generated summary metadata.
- Derived metadata may lag, but lag is visible and bounded by revision counters.
- No summary or embedding task can be permanently stuck in `pending` or `running`, because no such task rows exist.
- Recent visible sessions are enriched before historical maintenance work.
- Embeddings and backfill cannot delay live titles/summaries.
- Provider API concurrency is governed by explicit remote-call budgets, not by a generic "cold worker" thread count.
- `transcript_revision` increment and `needs_embedding=1` are committed atomically with transcript-changing event inserts.

## Non-Goals

- Do not replace SQLite. The redesign should work with the current single-writer model.
- Do not build a general job system.
- Do not require summaries to exist before sessions appear on the timeline.
- Do not make embeddings part of the live card path.
- Do not require a destructive cleanup migration for old `summary`/`embedding` `SessionTask` rows.

## Design

### 1. Session Row State Is the Queue

Summary candidates are selected from `sessions`:

```sql
SELECT id
FROM sessions
WHERE COALESCE(transcript_revision, 0) > COALESCE(summary_revision, 0)
  AND COALESCE(transcript_revision, 0) > 0
  AND COALESCE(user_messages, 0) + COALESCE(assistant_messages, 0) >= 2
ORDER BY
  CASE WHEN summary_title IS NULL OR summary_title = '' THEN 0 ELSE 1 END,
  last_activity_at DESC,
  started_at DESC
LIMIT :batch_size;
```

Embedding candidates remain derivable from `sessions`:

```sql
SELECT id
FROM sessions
WHERE COALESCE(needs_embedding, 1) = 1
   OR COALESCE(transcript_revision, 0) > COALESCE(embedding_revision, 0)
ORDER BY last_activity_at DESC, started_at DESC
LIMIT :batch_size;
```

No enqueue step is needed after ingest. Ingest only updates raw events and revision counters.

### 2. Live Summary Reconciler

Add a summary reconciliation loop that:

- polls for stale summary candidates,
- prefers sessions with no title and recent activity,
- runs provider calls concurrently behind a summary-specific semaphore,
- tracks an in-process `set[session_id]` of active summary reconciliations so concurrent scans do not duplicate provider calls,
- always releases the in-process active claim in `finally`, including cancellation paths,
- never holds DB sessions during provider calls,
- writes results with a revision guard,
- advances `summary_revision` when a session has too little meaningful content or no new durable events.

Provider concurrency should be a named budget such as `SESSION_SUMMARY_CONCURRENCY`, not `COLD_INGEST_WORKER_CONCURRENCY`. The default should reflect remote API behavior and hosted cost control, not local CPU.

The worker should call existing `generate_summary_impl(session_id)`. Live summary writes must continue using the `WriteSerializer` label `summary` so they stay ahead of backfill writes; do not route live work through the lower-priority `summary-backfill` label.

### 3. Embeddings Stay Manual/Backfill For Launch

Embedding reconciliation is not part of live timeline enrichment. For launch, keep explicit `/api/agents/backfill-embeddings` as the embedding maintenance path.

Rationale:

- The observed incident affected visible summary/title freshness; embedding lag was collateral.
- Search/recall quality degrades gracefully when embeddings lag; timeline comprehension does not depend on embeddings.
- `needs_embedding=1` remains durable state, so no work is lost.
- Adding an automatic embedding scanner now would duplicate the summary reconciler's concurrency, duplicate-call avoidance, and backpressure machinery before recall freshness is the primary launch constraint.

Deployment requirement: after summary/embedding task enqueueing is removed, hosted ops must run embedding backfill on a cron or manual cadence until a future product need justifies an automatic embedding scanner.

If a future always-on embedding reconciler is added, it must:

- have its own provider semaphore,
- rank recent sessions first,
- back off when live summary work is active,
- never share a generic queue with summary work,
- never influence timeline card status,
- use normal read sessions plus `WriteSerializer`, not a second always-on write path patterned after the explicit backfill route.

### 4. Summary Status Projection Stops Reading `SessionTask`

API `summary_status` should be derived from `sessions` only:

```text
ready       summary is non-empty after trimming whitespace
pending     summary is empty, transcript_revision > 0, enough content exists, and summary_revision < transcript_revision
unavailable summary is empty and content is too small, transcript_revision is 0, or summary_revision >= transcript_revision
failed      reserved for explicit future terminal enrichment errors, not derived from old SessionTask rows
```

This keeps UI state tied to actual session enrichment lag rather than queue internals. Existing old `summary` task rows become irrelevant.

Truth table checkpoints:

- Non-empty summary wins and returns `ready` even if revision counters lag.
- Whitespace-only summary is empty.
- `summary_revision >= transcript_revision` with `summary=null` is legitimate after low-content/no-new-events fast-forward and returns `unavailable`.
- Old `SessionTask(summary)` rows must not affect the result.

### 5. Ingest Does Not Enqueue Summary/Embedding Tasks

When ingest changes durable transcript content:

- insert event/source-line rows,
- increment `transcript_revision`,
- set `needs_embedding = 1`,
- update cheap card fields such as `last_activity_at`,
- commit those changes in the same ingest transaction,
- do not insert `summary` or `embedding` `SessionTask` rows.

If no durable transcript content changed, ingest must not mark summary or embedding work dirty.

### 6. Ingest Task Worker Removal

After summary and embedding no longer use `SessionTask`, remove the generic ingest task worker runtime path:

- stop starting the cold ingest task worker from lifespan,
- remove `COLD_INGEST_WORKER_CONCURRENCY`,
- remove summary/embed timeout tiers from `ingest_task_queue`,
- remove summary/embed claim priority, execution branches, retry, and resurrection code,
- remove dormant hot `turn_loop` worker scaffolding unless a real production consumer lands first,
- update comments/tests so `SessionTask` is not advertised as active summary/embedding infrastructure.

`SessionTask` model/table may remain inert for compatibility and future cleanup, but production runtime should not claim or execute `task_type in ('summary', 'embedding')`.

### 7. Existing Backlog Cleanup

Old `summary` and `embedding` `SessionTask` rows may remain in hosted tenant DBs. The runtime should ignore them after API projection and workers stop reading them.

Optional cleanup can be a one-shot maintenance function or migration later:

```sql
UPDATE session_tasks
SET status = 'done', updated_at = CURRENT_TIMESTAMP
WHERE task_type IN ('summary', 'embedding')
  AND status IN ('pending', 'running');
```

This cleanup is not required for correctness once projection and workers ignore those task types.

## Decisions

### Decision: Revision Counters Are the Work Contract

Context: Summary and embedding task rows duplicate information already represented by `transcript_revision`, `summary_revision`, `embedding_revision`, and `needs_embedding`.

Choice: Treat revision lag as the source of truth for stale enrichment.

Rationale: This removes task-row lifecycle failure modes and makes stale derived metadata reconstructable from session state.

Revisit if: enrichment work needs per-attempt audit history for billing, abuse control, or explicit user-visible failure reporting.

### Decision: Summary and Embeddings Are Separate Product Lanes

Context: Timeline comprehension and semantic recall have different latency requirements.

Choice: Summary/title enrichment gets a live lane; embeddings remain explicit maintenance/backfill for launch.

Rationale: A user can see missing title/summary immediately. Embedding freshness matters for search quality but should not block or slow visible cards.

Revisit if: recall becomes the primary launch surface and needs fresh embeddings within seconds.

### Decision: Delete Dormant Worker Scaffolding Unless A Real Producer Appears

Context: Hatch Opus review found no active `turn_loop` producer or `_run_task_impl` branch despite the hot worker scaffolding.

Choice: Phase 4 should remove the generic ingest task worker runtime path, including hot worker scaffolding, unless implementation discovers an active production consumer.

Rationale: Keeping unused workers preserves the mental model that this is a general queue. The goal is to remove that abstraction for launch.

Revisit if: a concrete `turn_loop` producer lands before Phase 4 and needs durable queue semantics.

### Decision: Ignore Old Summary/Embedding Task Rows

Context: Hosted DBs may already have thousands of old rows.

Choice: Make runtime correctness independent of those rows; cleanup later is optional.

Rationale: Ignoring stale queue rows is safer than relying on a bulk mutation during rollout.

Revisit if: old rows create storage/performance issues.

### Decision: Phase 2 And Phase 3 Must Land Together Or Phase 3 Lands First

Context: If ingest stops enqueueing summary tasks before a revision-lag reconciler exists, new sessions may not be summarized.

Choice: Build projection first, then add the live summary reconciler before or in the same phase as removing summary enqueueing.

Rationale: This avoids a regression window while still moving toward deletion.

Revisit if: a hotfix explicitly needs only the additive reconciler first.

## Implementation Phases

### Phase 0: Spec and Review

Acceptance criteria:

- This spec exists under `docs/specs/`.
- Hatch Opus reviews the design before code implementation.
- Review feedback is folded into this spec.
- Embeddings are explicitly decided as manual/backfill-only for launch.
- Spec is committed independently.

Test commands:

- Documentation-only phase; no test command required.

### Phase 1: Project Summary Status From Session Revisions

Goal: Decouple API/UI summary state from `SessionTask`.

Work:

- Remove `SessionTask` summary lookup from `session_response_projection.py`.
- Delete `SUMMARY_TERMINAL_RESURRECTION_COUNT` and the comment linking projection to ingest task resurrection.
- Derive `summary_status` from `summary`, `summary_revision`, `transcript_revision`, and message counts.
- Update `server/tests_lite/test_session_summary_status.py` around revision lag instead of task rows.
- Keep `failed` reserved but unreachable unless a future explicit field exists.

Acceptance criteria:

- A session with `summary=null`, enough content, `transcript_revision > 0`, and `summary_revision < transcript_revision` projects `summary_status=pending`.
- A session with a non-empty summary projects `ready` even if revision counters lag.
- A whitespace-only summary is not `ready`.
- A session with too little content projects `unavailable`.
- A session with `summary=null` and `summary_revision >= transcript_revision` projects `unavailable`.
- A session with `transcript_revision=0` projects `unavailable`.
- Old `SessionTask(summary)` rows do not affect `summary_status`.

Test commands:

```bash
cd server && DATABASE_URL=sqlite:////tmp/longhouse-test-$(uuidgen).db AUTH_DISABLED=1 SKIP_DEMO_SEED=1 FERNET_SECRET=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())') TRIGGER_SIGNING_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') OPENROUTER_API_KEY=test-openrouter-key uv run pytest tests_lite/test_session_summary_status.py tests_lite/test_session_revision_guards.py
cd server && uv run ruff check zerg/services/session_response_projection.py tests_lite/test_session_summary_status.py
```

### Phase 2: Add Summary Revision-Lag Reconciler

Goal: Add the non-task live summary path before removing task enqueueing.

Work:

- Add a small service for stale summary candidate selection and worker loop.
- Start it from lifespan with a named summary concurrency setting.
- Use existing `generate_summary_impl(session_id)` for per-session work.
- Track in-process active session IDs to avoid duplicate provider calls across concurrent scans.
- Ensure candidate selection never requires or mutates `SessionTask`.
- Add tests for candidate ordering, stale/current filtering, duplicate-call avoidance, cancellation cleanup, and concurrency caps.

Acceptance criteria:

- Stale recent sessions are selected before older stale sessions.
- Missing-title stale sessions are selected before title-present stale sessions.
- Sessions with no meaningful content are handled without provider calls and become current or unavailable according to existing summary logic.
- Concurrent worker iterations do not duplicate provider calls for the same stale revision.
- Active in-process claims are released in `finally`, including cancellation.
- Summary writes are revision guarded.
- The worker and scan loop do not hold DB connections while provider calls are running.
- Live work uses `WriteSerializer` label `summary`, not `summary-backfill`.
- Reconciler concurrency never exceeds `SESSION_SUMMARY_CONCURRENCY`.
- With many stale old `SessionTask(summary)` rows present, the reconciler still advances stale sessions without reading `SessionTask`.

Test commands:

```bash
cd server && DATABASE_URL=sqlite:////tmp/longhouse-test-$(uuidgen).db AUTH_DISABLED=1 SKIP_DEMO_SEED=1 FERNET_SECRET=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())') TRIGGER_SIGNING_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') OPENROUTER_API_KEY=test-openrouter-key uv run pytest tests_lite/test_session_enrichment_reconciler.py tests_lite/test_session_revision_guards.py
cd server && uv run ruff check zerg/services/session_enrichment_reconciler.py tests_lite/test_session_enrichment_reconciler.py
```

### Phase 3: Stop Enqueueing Summary/Embedding Tasks on Ingest

Goal: Ingest writes raw truth and dirty revision markers only.

Work:

- Change ingest behavior so transcript-changing ingest no longer inserts `summary` or `embedding` `SessionTask` rows.
- Preserve `transcript_revision` increment and `needs_embedding=1`.
- Keep revision/dirty marker writes in the same transaction as event/source-line inserts.
- Update ingest duplicate/replay tests that currently expect two pending task rows.
- Update `test_duplicate_replay_without_source_line_delta_does_not_requeue_post_ingest_work` to assert no summary/embed tasks are created on either pass.
- Delete or rewrite summary/embed-specific enqueue/dedup tests in `test_ingest_task_queue.py`.

Acceptance criteria:

- New transcript-changing ingest increments `transcript_revision`.
- New transcript-changing ingest sets `needs_embedding=1`.
- Event insert, `transcript_revision` increment, and `needs_embedding=1` are committed atomically in the same ingest transaction.
- New transcript-changing ingest creates no `summary` or `embedding` task rows.
- Existing duplicate/replay behavior remains idempotent.
- Embedding debt remains visible via `needs_embedding=1` for explicit backfill.

Test commands:

```bash
cd server && DATABASE_URL=sqlite:////tmp/longhouse-test-$(uuidgen).db AUTH_DISABLED=1 SKIP_DEMO_SEED=1 FERNET_SECRET=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())') TRIGGER_SIGNING_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') OPENROUTER_API_KEY=test-openrouter-key uv run pytest tests_lite/test_agents_duplicate_sqlite.py tests_lite/test_session_revision_guards.py tests_lite/test_session_enrichment_reconciler.py
cd server && uv run ruff check tests_lite/test_agents_duplicate_sqlite.py
```

### Phase 4: Remove Generic Ingest Task Worker Runtime Paths

Goal: Delete the generic worker surface that made summary/embedding look like cold queued work.

Work:

- Stop starting ingest task workers from lifespan.
- Remove `COLD_INGEST_WORKER_CONCURRENCY`.
- Remove summary/embed timeout tiers, claim priority, execution branches, and related retry/resurrection code.
- Remove `_hot_worker_event`, `_notify_hot_worker`, `_wait_for_hot_worker_signal`, `HOT_INGEST_TASK_TYPES`, and hot worker startup unless a real production consumer lands first.
- Rename or update comments so `SessionTask` is not described as active summary/embed infrastructure.
- Remove/replace obsolete concurrent cold worker tests.
- Add a grep-style regression test or equivalent check that production code outside legacy/model definitions does not reference `task_type == "summary"` or `task_type == "embedding"`.

Acceptance criteria:

- No runtime code claims or executes `SessionTask(task_type in ('summary', 'embedding'))`.
- No environment variable named `COLD_INGEST_WORKER_CONCURRENCY` remains.
- `SessionTask` comments do not advertise summary/embedding as active users.
- Lifespan starts the summary reconciler but no generic ingest task workers.
- Importing runtime modules does not reference summary/embed task queue literals except compatibility/model comments.

Test commands:

```bash
cd server && DATABASE_URL=sqlite:////tmp/longhouse-test-$(uuidgen).db AUTH_DISABLED=1 SKIP_DEMO_SEED=1 FERNET_SECRET=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())') TRIGGER_SIGNING_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') OPENROUTER_API_KEY=test-openrouter-key uv run pytest tests_lite/test_session_enrichment_reconciler.py tests_lite/test_session_summary_status.py tests_lite/test_embeddings.py tests_lite/test_session_enrichment_architecture.py
cd server && uv run ruff check zerg/lifespan.py zerg/models/agents.py zerg/services/session_summaries.py tests_lite/test_session_enrichment_architecture.py
```

### Phase 5: Embedding Manual Backfill Verification

Goal: Preserve search/recall quality without introducing an automatic embedding scanner.

Work:

- Verify `/api/agents/backfill-embeddings` still drains `needs_embedding=1` rows after ingest stops enqueueing embedding tasks.
- Add or update tests showing `needs_embedding=1` can accumulate and explicit backfill clears it.
- Document the hosted ops requirement for periodic embedding backfill if an appropriate ops doc exists.

Acceptance criteria:

- Embedding work does not use `SessionTask`.
- Embedding work cannot block summary/title reconciliation.
- `needs_embedding=1` rows are drained by explicit backfill.
- Search/recall tests still pass.

Test commands:

```bash
cd server && DATABASE_URL=sqlite:////tmp/longhouse-test-$(uuidgen).db AUTH_DISABLED=1 SKIP_DEMO_SEED=1 FERNET_SECRET=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())') TRIGGER_SIGNING_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') OPENROUTER_API_KEY=test-openrouter-key uv run pytest tests_lite/test_agents_backfill_embeddings.py tests_lite/test_embeddings.py tests_lite/test_session_revision_guards.py tests_lite/test_agents_duplicate_sqlite.py
cd server && uv run ruff check tests_lite/test_agents_backfill_embeddings.py
```

### Phase 6: Final Integration Review

Acceptance criteria:

- `make test` passes, or any failures are proven unrelated.
- Hatch Opus performs an end-to-end architecture review.
- Spec status is updated to `Implemented`.
- Docket item is closed.

Test commands:

```bash
make test
```

## Implementation Result

Implemented in focused commits:

- `caaf9ab3` derives timeline summary status from session revisions and ignores old summary tasks.
- `79574818` adds the summary revision-lag reconciler; `fd677b7a` isolates sibling failures in a reconciler batch.
- `9ede29c4` stops ingest from enqueueing summary/embedding tasks.
- `a257fa96` removes the generic ingest task worker runtime path.
- `3c73663d` adds a regression guard against reintroducing summary/embedding task queue usage.
- `450b9307` verifies explicit embedding backfill drains `needs_embedding` rows.

Verification:

- `make test`: `1719 passed, 1 skipped`.
- Focused phase tests and ruff checks passed as each phase landed.
- Hatch Opus approved Phases 1, 2, 3, 4, and the final end-to-end implementation review.

## Rollout Notes

- Hosted tenants can deploy this without a schema migration because existing revision fields already exist.
- Old `summary`/`embedding` task rows become ignored runtime debris.
- If the hosted DB has many old rows, cleanup can happen after rollout.
- After Phase 3, hosted ops must run `/api/agents/backfill-embeddings` on a cron or manual cadence until a future product need justifies an automatic embedding scanner.
- If this touches iOS only through already-committed timeline fallback, David still needs an Xcode install for phone dogfood after that commit lands.

## Hatch Opus Review Resolution

Hatch Opus returned `APPROVE WITH FIXES`. Incorporated fixes:

- revision counters remain the work contract,
- embedding automatic scanner is deferred; manual backfill is the launch decision,
- `summary_status` derives from session revision state and handles fast-forwarded empty summaries,
- duplicate provider-call avoidance is required via an in-process active set,
- live summary work must use the `summary` write label,
- Phase 2/3 ordering is corrected so the reconciler lands before ingest stops enqueueing,
- dormant hot-worker scaffolding is no longer preserved by default,
- tests now explicitly cover empty summaries, duplicate-call avoidance, old task backlog ignorance, and backfill verification.
