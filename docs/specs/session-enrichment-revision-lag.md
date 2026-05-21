# Session Enrichment Revision-Lag Reconciliation

Status: Draft for Hatch Opus review
Owner: David Rose
Created: 2026-05-20

## Executive Summary

Longhouse should treat raw session ingest as the product truth and derived session metadata as opportunistic enrichment. The current post-ingest pipeline creates durable `SessionTask` rows for summary and embedding work, then drains those rows through a cold worker. This made remote API work behave like a local single-lane queue: live summary/title work, embeddings, historical reingest, retries, and backfill all competed behind the same worker and the same SQLite write serializer.

Replace summary and embedding `SessionTask` usage with revision-lag reconciliation:

```text
summary work is needed when summary_revision < transcript_revision
embedding work is needed when embedding_revision < transcript_revision or needs_embedding = 1
```

The database state is the queue. Workers scan session rows, choose the highest-value stale sessions, run bounded concurrent remote API calls, and advance the relevant revision only if the write still matches the observed transcript revision. A missing summary/title never blocks the timeline: cards use stored summary/title when available and deterministic fallbacks otherwise.

`SessionTask` can remain for `turn_loop` while summary and embedding enrichment move out of it. The goal is deletion of the generic cold queue, not a bigger queue abstraction.

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

Problematic current shape:

- `enqueue_ingest_tasks()` inserts `summary` and `embedding` `SessionTask` rows after every transcript-changing ingest.
- The cold worker defaults to one concurrent task and claims one task at a time.
- Summary, embedding, and historical reingest-derived work share that cold lane.
- Task claim/done/timeout writes go through `WriteSerializer`, so heavy archive ingest can delay even tiny bookkeeping writes.
- `summary_status` projection currently reads latest `SessionTask(summary)` rows, so old/pending task rows leak queue internals into API/UI semantics.
- Retry/resurrection logic exists for summary/embedding even though revision counters already describe whether the derived state is stale.

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

## Non-Goals

- Do not remove `SessionTask` entirely in this project. It still supports `turn_loop`.
- Do not replace SQLite. The redesign should work with the current single-writer model.
- Do not build a general job system.
- Do not require summaries to exist before sessions appear on the timeline.
- Do not make embeddings part of the live card path.

## Design

### 1. Session Row State Is the Queue

Summary candidates are selected from `sessions`:

```sql
SELECT id
FROM sessions
WHERE COALESCE(transcript_revision, 0) > COALESCE(summary_revision, 0)
  AND COALESCE(user_messages, 0) + COALESCE(assistant_messages, 0) >= 2
ORDER BY
  CASE WHEN summary_title IS NULL OR summary_title = '' THEN 0 ELSE 1 END,
  last_activity_at DESC,
  started_at DESC
LIMIT :batch_size;
```

Embedding candidates are selected from `sessions`:

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
- never holds DB sessions during provider calls,
- writes results with a revision guard,
- advances `summary_revision` when a session has too little meaningful content or no new durable events.

Provider concurrency should be a named budget such as `SESSION_SUMMARY_CONCURRENCY`, not `COLD_INGEST_WORKER_CONCURRENCY`. The default should reflect remote API behavior and hosted cost control, not local CPU.

The worker should call existing `generate_summary_impl(session_id)` first. If stale writes or CAS conflicts are found during implementation, prefer tightening revision guards in `generate_summary_impl` over creating task rows.

### 3. Embedding Reconciler Is Separate Maintenance

Embedding reconciliation is not part of live timeline enrichment.

Options:

- Keep explicit `/api/agents/backfill-embeddings` as the main embedding maintenance path.
- Add a low-priority embedding reconciler only if recall/search freshness needs it.

If an always-on embedding reconciler is kept, it must:

- have its own provider semaphore,
- rank recent sessions first,
- back off when live summary work is active,
- never share a generic queue with summary work,
- never influence timeline card status.

### 4. Summary Status Projection Stops Reading `SessionTask`

API `summary_status` should be derived from `sessions` only:

```text
ready       summary is non-empty
pending     summary is empty, enough content exists, and summary_revision < transcript_revision
unavailable summary is empty and content is too small or transcript_revision is current
failed      reserved for explicit future terminal enrichment errors, not derived from old SessionTask rows
```

This keeps UI state tied to actual session enrichment lag rather than queue internals. Existing old `summary` task rows become irrelevant.

### 5. Ingest Does Not Enqueue Summary/Embedding Tasks

When ingest changes durable transcript content:

- increment `transcript_revision`,
- set `needs_embedding = 1`,
- update cheap card fields such as `last_activity_at`,
- do not insert `summary` or `embedding` `SessionTask` rows.

`turn_loop` task behavior may stay as-is until separately simplified.

### 6. Cold Worker Removal

After summary and embedding no longer use `SessionTask`, remove the cold worker startup path:

- keep the hot `turn_loop` worker if still needed,
- remove `COLD_INGEST_WORKER_CONCURRENCY`,
- remove summary/embed timeout tiers from `ingest_task_queue`,
- remove summary/embed retry/resurrection logic from `ingest_task_queue`,
- update comments and tests so the module is no longer described as summary/embed infrastructure.

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

Choice: Summary/title enrichment gets a live lane; embeddings remain maintenance/backfill.

Rationale: A user can see missing title/summary immediately. Embedding freshness matters for search quality but should not block or slow visible cards.

Revisit if: recall becomes the primary launch surface and needs fresh embeddings within seconds.

### Decision: Keep `SessionTask` for `turn_loop` Initially

Context: `SessionTask` also supports `turn_loop` and removing that in the same change would widen the blast radius.

Choice: Remove summary/embedding use of `SessionTask` first; leave turn-loop queue behavior intact.

Rationale: This delivers the queue simplification that caused the incident without mixing in unrelated loop behavior.

Revisit if: a later review shows `turn_loop` can also be represented by direct runtime/session state.

### Decision: Ignore Old Summary/Embedding Task Rows

Context: Hosted DBs may already have thousands of old rows.

Choice: Make runtime correctness independent of those rows; cleanup later is optional.

Rationale: Ignoring stale queue rows is safer than relying on a bulk mutation during rollout.

Revisit if: old rows create storage/performance issues.

## Implementation Phases

### Phase 0: Spec and Review

Acceptance criteria:

- This spec exists under `docs/specs/`.
- Hatch Opus reviews the design before code implementation.
- Review feedback is folded into this spec.
- Spec is committed independently.

Test commands:

- Documentation-only phase; no test command required.

### Phase 1: Project Summary Status From Session Revisions

Goal: Decouple API/UI summary state from `SessionTask`.

Work:

- Remove `SessionTask` summary lookup from `session_response_projection.py`.
- Derive `summary_status` from `summary`, `summary_revision`, `transcript_revision`, and message counts.
- Update `server/tests_lite/test_session_summary_status.py` around revision lag instead of task rows.
- Keep `failed` reserved but unreachable unless a future explicit field exists.

Acceptance criteria:

- A session with `summary=null`, enough content, and `summary_revision < transcript_revision` projects `summary_status=pending`.
- A session with a non-empty summary projects `ready` even if revision counters lag.
- A session with too little content projects `unavailable`.
- Old `SessionTask(summary)` rows do not affect `summary_status`.

Test commands:

```bash
cd server && DATABASE_URL=sqlite:////tmp/longhouse-test-$(uuidgen).db AUTH_DISABLED=1 SKIP_DEMO_SEED=1 FERNET_SECRET=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())') TRIGGER_SIGNING_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') OPENROUTER_API_KEY=test-openrouter-key uv run pytest tests_lite/test_session_summary_status.py tests_lite/test_session_revision_guards.py
cd server && uv run ruff check zerg/services/session_response_projection.py tests_lite/test_session_summary_status.py
```

### Phase 2: Stop Enqueueing Summary/Embedding Tasks on Ingest

Goal: Ingest writes raw truth and dirty revision markers only.

Work:

- Change `enqueue_ingest_tasks()` or ingest caller behavior so transcript-changing ingest no longer inserts `summary` or `embedding` `SessionTask` rows.
- Preserve `transcript_revision` increment and `needs_embedding=1`.
- Update ingest duplicate/replay tests that currently expect two pending task rows.
- Keep any `turn_loop` enqueueing behavior separate if it exists.

Acceptance criteria:

- New transcript-changing ingest increments `transcript_revision`.
- New transcript-changing ingest sets `needs_embedding=1`.
- New transcript-changing ingest creates no `summary` or `embedding` task rows.
- Existing duplicate/replay behavior remains idempotent.

Test commands:

```bash
cd server && DATABASE_URL=sqlite:////tmp/longhouse-test-$(uuidgen).db AUTH_DISABLED=1 SKIP_DEMO_SEED=1 FERNET_SECRET=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())') TRIGGER_SIGNING_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') OPENROUTER_API_KEY=test-openrouter-key uv run pytest tests_lite/test_agents_duplicate_sqlite.py tests_lite/test_ingest_task_queue.py tests_lite/test_session_revision_guards.py
cd server && uv run ruff check zerg/services/agents/store.py zerg/services/ingest_task_queue.py tests_lite/test_agents_duplicate_sqlite.py tests_lite/test_ingest_task_queue.py
```

### Phase 3: Add Summary Revision-Lag Reconciler

Goal: Replace summary task draining with direct session scanning.

Work:

- Add a small service for stale summary candidate selection and worker loop.
- Start it from lifespan with a named summary concurrency setting.
- Use existing `generate_summary_impl(session_id)` for per-session work.
- Ensure candidate selection never requires or mutates `SessionTask`.
- Add tests for candidate ordering, stale/current filtering, and concurrency/claim behavior.

Acceptance criteria:

- Stale recent sessions are selected before older stale sessions.
- Sessions with no meaningful content are handled without provider calls and become current or unavailable according to existing summary logic.
- Concurrent worker iterations do not permanently duplicate provider calls for the same stale revision.
- Summary writes are revision guarded.
- The worker does not hold DB connections while provider calls are running.

Test commands:

```bash
cd server && DATABASE_URL=sqlite:////tmp/longhouse-test-$(uuidgen).db AUTH_DISABLED=1 SKIP_DEMO_SEED=1 FERNET_SECRET=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())') TRIGGER_SIGNING_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') OPENROUTER_API_KEY=test-openrouter-key uv run pytest tests_lite/test_session_enrichment_reconciler.py tests_lite/test_session_revision_guards.py
cd server && uv run ruff check zerg/services/session_enrichment_reconciler.py tests_lite/test_session_enrichment_reconciler.py
```

### Phase 4: Remove Cold Summary/Embedding Task Queue Paths

Goal: Delete the generic cold worker surface for summary/embedding.

Work:

- Stop starting the cold ingest task worker from lifespan.
- Keep hot `turn_loop` worker if still used.
- Remove `COLD_INGEST_WORKER_CONCURRENCY`, summary/embed timeout tiers, summary/embed claim priority, summary/embed execution branches, and related retry/resurrection code where no longer used.
- Rename or update comments so `ingest_task_queue.py` describes the remaining `turn_loop` queue only.
- Remove/replace obsolete concurrent cold worker tests.

Acceptance criteria:

- No runtime code claims or executes `SessionTask(task_type in ('summary', 'embedding'))`.
- No environment variable named `COLD_INGEST_WORKER_CONCURRENCY` remains.
- `SessionTask` comments do not advertise summary/embedding as active users.
- Turn-loop tests still pass.

Test commands:

```bash
cd server && DATABASE_URL=sqlite:////tmp/longhouse-test-$(uuidgen).db AUTH_DISABLED=1 SKIP_DEMO_SEED=1 FERNET_SECRET=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())') TRIGGER_SIGNING_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') OPENROUTER_API_KEY=test-openrouter-key uv run pytest tests_lite/test_ingest_task_queue.py tests_lite/test_session_enrichment_reconciler.py tests_lite/test_session_summary_status.py
cd server && uv run ruff check zerg/services/ingest_task_queue.py zerg/lifespan.py tests_lite/test_ingest_task_queue.py
```

### Phase 5: Embedding Lane Decision

Goal: Keep embeddings from blocking live enrichment while preserving search/recall quality.

Work:

- Decide whether to leave embeddings as explicit `/backfill-embeddings` only or add a low-priority direct session scanner.
- If adding a scanner, implement it in a separate service with its own budget and tests.
- Update backfill docs/API behavior if `needs_embedding` semantics change.

Acceptance criteria:

- Embedding work does not use `SessionTask`.
- Embedding work cannot block summary/title reconciliation.
- Search/recall tests still pass.

Test commands:

```bash
cd server && DATABASE_URL=sqlite:////tmp/longhouse-test-$(uuidgen).db AUTH_DISABLED=1 SKIP_DEMO_SEED=1 FERNET_SECRET=$(python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())') TRIGGER_SIGNING_SECRET=$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))') OPENROUTER_API_KEY=test-openrouter-key uv run pytest tests_lite/test_embeddings.py tests_lite/test_session_revision_guards.py
cd server && uv run ruff check zerg/services/session_summaries.py zerg/services/session_processing/embeddings.py zerg/routers/agents_backfill.py
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

## Rollout Notes

- Hosted tenants can deploy this without a schema migration because existing revision fields already exist.
- Old `summary`/`embedding` task rows become ignored runtime debris.
- If the hosted DB has many old rows, cleanup can happen after rollout.
- If this touches iOS only through already-committed timeline fallback, David still needs an Xcode install for phone dogfood after that commit lands.

## Open Review Questions For Hatch Opus

- Is there any remaining reason summary/embedding need durable task rows rather than revision-lag scanning?
- Should Phase 3 include an explicit short-lived in-process claim set to avoid duplicate provider calls across concurrent worker loops?
- Should embedding reconciliation stay manual/backfill-only for launch?
- Is `summary_status=pending` from revision lag useful to clients now that the iOS card no longer renders "Generating summary"?
- What is the smallest safe first implementation slice that removes the observed failure mode without destabilizing ingest?
