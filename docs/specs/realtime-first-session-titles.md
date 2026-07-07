# Realtime-First Session Titles

Status: Draft
Last updated: 2026-07-07

## Executive Summary

The first stable session title should be treated as part of the first-message
lifecycle, not as an eventual enrichment artifact.

Today a new session title can lag because two polling loops sit between the
first durable user message and the UI seeing the AI-generated title:

1. The summary reconciler wakes every `SESSION_SUMMARY_POLL_SECONDS` (default
   5s) and scans for sessions with a first user message but no title.
2. `generate_initial_title_impl()` writes `summary_title` and `anchor_title`,
   but does not publish a realtime update. Timeline/detail streams therefore
   only notice through their own durable-signature checks or if another event
   wakes them. The detail/workspace signature must explicitly include title
   state for a title-only write to be a reliable realtime invalidation.

The target behavior is:

```
session created -> first message committed -> first-message fallback visible
               -> post-commit title task starts immediately -> title persisted
               -> title signature changes -> title update published
               -> web/iOS refetch immediately
```

The LLM call must stay out of the user send transaction. The trigger belongs at
the post-commit ingest/write boundary, after the first user event is durable,
not inside projection repair code.

## Product Goal

When a user starts a session from web or iOS and sends the first message, the UI
should feel like it understands the new session immediately:

- Before the first message: show a structured fallback, never blank.
- After first message commit: show a sanitized first-message fallback quickly.
- After title model completion: swap to the frozen stable title quickly.

Normal target: title visible in roughly the model latency plus one refetch, with
no built-in 5s scan delay and no built-in 5s stream-poll delay.

## Non-Goals

- Do not block send, launch, ingest, or transcript persistence on an LLM call.
- Do not generate a new stable title after every message.
- Do not make clients implement their own title fallback ladders.
- Do not introduce visible spinners or animated title transitions in v1.
- Do not remove the summary reconciler. It remains the recovery/backfill path.
- Do not generate titles for sessions with no meaningful user message.

## Current System Walkthrough

### 1. Session Creation

A session row is created by one of several paths:

- managed local launch
- remote launch from web/iOS
- provider ingest/discovery for Shadow sessions
- continued/adopted sessions

At creation time, the session may have no user message. The server-side title
resolver falls back to project/branch/provider-level context. Web has a
compatibility fallback for older payloads. iOS timeline rows now use the same
server-resolved title path; iOS detail also carries that resolved title through
`SessionDetail.title`.

### 2. First User Message

The first user message becomes durable as an `AgentEvent(role="user")`.
Projection code updates denormalized session fields:

- `user_messages`
- `transcript_revision`
- `first_user_message_preview`
- timeline hot-card fields

The ingest/control paths publish session and timeline pubsub wakes for durable
message changes. This is why clients can quickly show the first-message fallback
once the first user message is committed.

### 3. AI Title Selection

The current title generator is reached only through
`run_summary_reconciler()`:

- `select_initial_title_session_ids()` finds low-content sessions with
  `first_user_message_preview` present and no `summary_title`/`anchor_title`.
- `generate_initial_title_impl(session_id)` calls the title model.
- It persists:
  - `summary_title`
  - `anchor_title`
  - `summary_revision`
  - hot timeline card mirror

This path is correct for eventual consistency, but it is not realtime. It is a
periodic scanner.

### 4. UI Visibility After Title Write

Title persistence currently updates durable state but does not publish a
session or timeline wake.

Consequences:

- Timeline SSE eventually discovers the changed payload through its timeout
  poll.
- Workspace/detail SSE is gated by `SessionWorkspaceRevision.signature`. That
  signature currently includes event, session timestamp, runtime, pause, control,
  and live-preview state, but not title fields directly. A title-only write must
  move this signature intentionally; relying on incidental `updated_at` movement
  is too implicit for this UX path.
- If some other event happens soon, the UI may update quickly by accident.
- If the session is otherwise quiet, title visibility can be delayed by the
  stream fallback interval.

## Why The Lag Exists

The lag is not a model-speed problem by itself. The title model may return in
hundreds of milliseconds, but the model call is surrounded by polling:

```
first message commit
  -> wait up to 5s for summary reconciler tick
  -> model call
  -> title DB write
  -> wait up to 5s for stream timeout poll
  -> client refetch
```

That makes the worst-case latency roughly:

```
reconciler poll delay + model latency + stream poll delay + refetch latency
```

The system diverged this way because initial title generation was added as a
kind of low-content summary enrichment. That is a good recovery mechanism, but
it is not the right primary path for a first-interaction UI affordance.

## First-Principles Speed-of-Light Design

The "speed of light" design starts from the user-visible invariant:

> The first durable user message is the first moment Longhouse has enough
> semantic signal to name the session.

Therefore the first durable user message should trigger the title job directly
from a post-commit hook.

### Ideal Flow

```
User sends first prompt
  -> send accepted / durable event written
  -> first_user_message_preview set
  -> session/timeline pubsub wake
  -> UI shows first-message fallback
  -> post-commit title task starts immediately
  -> model returns
  -> title persisted as summary_title + anchor_title
  -> workspace/timeline durable signatures include the new title
  -> session/timeline pubsub wake
  -> UI refetches and shows stable title
```

The LLM call remains outside the transaction. The only hot-path work is
detecting that the committed batch inserted the first durable user message and
starting a background title task after the commit has succeeded.

### Latency Budget

| Stage | Target |
| --- | ---: |
| first user commit to title task scheduled | < 100ms |
| title model call | p50 < 1s, p95 < 3s |
| title persist to pubsub wake | < 50ms |
| pubsub wake to client refetch/render | < 1s |

The backup reconciler can still be 5s because it is not the primary UX path.

## Target Architecture

### Tiny Post-Commit Title Trigger

Add a small helper near the existing title/reconciler services, for example:

`server/zerg/services/session_title_trigger.py`

Responsibilities:

- expose `maybe_start_initial_title_generation(session_id, reason)`
- start one fire-and-forget task that calls `generate_initial_title_impl()`
- dedupe in-process active work for a short TTL
- never raise into ingest/send callers
- record structured logs/metrics for scheduled, skipped, generated, failed

This is not a scheduler abstraction and not a durable queue in v1. It is a
post-commit convenience hook. The summary reconciler remains the durable recovery
mechanism. The in-process active set only prevents duplicate fast-trigger calls
inside the current process; database idempotency remains the final guard.

### Trigger Point

Trigger when an ingest/write path knows it has just committed the first durable
non-warmup user event for a session.

The trigger should not be owned by `first_user_message_preview`. That field is
derived state and can be repaired by projection/backfill code long after the
real first message arrived. Use it as an input/guard for title text, not as the
source of truth for the event edge.

The edge is:

```
had_no_durable_user_event_before and inserted_first_durable_user_event_now
```

Relevant code paths to inspect:

- `AgentsStore.ingest_events_for_session(...)`: already computes inserted event
  counts, first-user preview deltas, and commits ingest chunks.
- The exact commit seam inside `AgentsStore.ingest_events_for_session(...)`:
  ingest can commit in chunks. For the speed-of-light path, fire the title
  trigger after the chunk commit that inserted the first durable user event, not
  only after the entire ingest request returns to `routers/agents_ingest.py`.
- `routers/agents_ingest.py`: still useful as a safety/fallback call site
  because it publishes session/timeline pubsub only after the ingest write
  returned successfully, but it may be later than the first chunk commit.
- managed input durable-turn materialization, if it writes user events through a
  different path.
- remote launch initial prompt ingest path, if it creates a durable user event
  before provider output starts.

Preview setters are still important, but only to avoid accidental triggers from
projection repair:

- `AgentsStore._refresh_session_previews()`: recomputes previews and assigns
  `first_user_message_preview` unconditionally.
- `AgentsStore._apply_incremental_session_count_deltas()`: applies cheap ingest
  deltas and already has an edge-like `first_user_preview and not existing`
  guard.

Those projection paths should not independently fire model calls. If a repair
path discovers an old session with a missing preview/title, let the reconciler
handle it.

### Title Freshness Signature

Title updates must move every durable signature that gates a title-bearing UI:

- timeline stream window/card signature, so sidebar rows receive a
  `session_upsert`.
- workspace/detail signature, so focused web/iOS detail surfaces receive
  `workspace_changed`.

Add title-relevant session fields to `SessionWorkspaceRevision.signature`,
preferably the resolved title inputs rather than only `updated_at`:

```
anchor_title
```

`updated_at` can remain in the signature, but title correctness should not
depend on whether SQLAlchemy happened to bump it for a particular write path.
Avoid adding `summary_title`, `first_user_message_preview`, or
`summary_revision` to this signature in v1. The first-title path freezes
`anchor_title`, and that is the stable user-visible title input. Summary title
refreshes, preview backfills, and summary revision churn are broader than title
visibility and would cause avoidable `workspace_changed` wakes.

### Publish On Title Write

After `generate_initial_title_impl()` successfully persists a title, publish a
title update to both realtime topics:

```
session:{session_id}
timeline
```

Payload shape:

```json
{
  "kind": "title_update",
  "session_id": "...",
  "provider": "codex",
  "source": "initial_title"
}
```

Clients do not need a special title payload in v1. Existing timeline/workspace
streams can treat this as an invalidation wake and refetch the authoritative
session/card payload.

### Reconciler Remains

`run_summary_reconciler()` keeps its current scan:

- process restarted before async title task completed
- model call failed transiently
- fast-path title trigger disabled/misconfigured
- older sessions missing title
- provider ingest path missed the edge trigger

The reconciler should be understood as repair, not as the primary UX path.
Do not add reconciler/fast-trigger coordination in v1 unless duplicate title
model calls show up as a measured cost. If the reconciler races the fast
trigger, the database guard still makes correctness idempotent; avoiding the
occasional extra LLM call is optimization, not architecture.

## Client Contract

### Web Timeline

No new UI state required.

On `title_update` timeline wake, the existing stream should load the targeted
card and emit `session_upsert` if the payload signature changed.

### Web Detail

No new UI state required.

On `title_update` session wake, workspace stream must see a changed workspace
signature and emit `workspace_changed`. React Query invalidation then refetches
`agent-session-workspace` and the header title updates.

This is a Phase 1 contract, not a later hardening item: focused detail views are
where the title is most visible.

### iOS Timeline

No new UI state required.

Timeline stream applies the upserted card, and `SessionSummary.title` already
uses the same resolved title path.

### iOS Detail

No new UI state required after the recent title-path alignment.

`SessionDetail.displayTitle` now prefers the resolved `title` carried by the
API adapter, then compatibility fallbacks.

## Observability

Add or extend logs/metrics around:

- first title job scheduled
- skipped because title already exists
- skipped because no first user message
- model call elapsed ms
- title persist elapsed ms
- title publish emitted
- first-message-to-title-persist latency

Useful derived metric:

```
initial_title_latency_ms =
  title_persisted_at - first_user_message_committed_at
```

This is the number that tells us whether the user-visible flow is actually
fast.

## Failure Behavior

If title generation fails:

- first-message fallback remains visible
- fast trigger logs failure
- no user-facing error
- reconciler retries later according to existing behavior

If title generation succeeds but publish fails:

- title is durable
- UI may still update on stream timeout poll or next event
- reconciler should not regenerate because title exists
- publish failure should be logged

If multiple tasks race:

- fast-trigger in-flight state should prevent avoidable duplicate model calls
- DB write remains idempotent: skip if `summary_title` or `anchor_title` exists
- only the first successful persist wins
- later tasks become no-ops

## Implementation Plan

### Phase 1: Make Title Writes Realtime-Visible

Goal: remove the second 5s delay after title persistence.

Changes:

- add `publish_session_title_update(session_id, provider, source)` helper in
  `session_pubsub.py`, or reuse a generic publish helper with `kind`.
- update `SessionWorkspaceRevision.signature` so title-only writes move the
  focused workspace/detail signature intentionally.
- call it after `generate_initial_title_impl()` successfully persists a title.
- add tests that successful title generation publishes both session and
  timeline topics.
- add stream tests proving timeline and workspace/detail wake without timeout
  polling.

This phase is complete only when both the timeline row and focused detail title
can update from a title-only write. If only the timeline stream wakes, the user
still sees stale detail UI after opening the session.

### Phase 2: Trigger Title On First Durable User Message

Goal: remove the first 5s reconciler delay.

Changes:

- add a tiny `session_title_trigger.py` helper.
- call it from the post-commit write/ingest boundary that knows it inserted the
  first durable user event. For chunked ingest, this means the chunk commit seam
  in `AgentsStore`, not merely the router after the whole request returns.
- do not fire from preview backfill or projection repair paths.
- make the background task non-blocking and failure-contained.
- add dedupe tests for repeated post-commit trigger attempts.
- add tests for no scheduling when title already exists.

### Phase 3: Contract And Telemetry Hardening

Goal: make the title path measurable and durable enough for launch confidence.

Changes:

- add first-message-to-title-persist latency logging/metric.
- add an integration-style test for:
  - create session
  - ingest first user event
  - title job runs
  - title update publishes
  - timeline/detail payload returns new `timeline_title`

### Phase 4: Optional UX Polish

Only after the fast path works:

- consider a very subtle title-generation debug marker in dev tools, not in
  primary UI.
- consider immediate title generation for launch requests that already include
  an initial prompt.
- consider manual title override support as a separate schema/product spec.
  Do not repurpose the frozen `anchor_title` contract casually.

## Test Plan

Backend unit tests:

- `generate_initial_title_impl()` publishes on successful persist.
- no publish when no title generated.
- no publish when title already existed.
- title trigger dedupes concurrent requests for same session.
- title trigger ignores sessions with no first user message.
- first durable user event chunk commit triggers exactly once.
- preview refresh/backfill does not schedule title jobs.

Backend stream tests:

- timeline stream emits an upsert after title update publish.
- workspace stream emits `workspace_changed` after title update publish.
- workspace signature changes for title-only session update.

Client tests:

- existing web title fallback tests remain green.
- existing iOS title fallback tests remain green.
- no client-specific fallback divergence is reintroduced.

Smoke/dogfood:

- start a fresh managed session from web.
- send first message.
- observe row/header:
  - immediate first-message fallback
  - stable AI title shortly after
- repeat on iOS after Xcode install.

## Open Questions For Review

1. Should `title_update` carry the new title payload for faster client patching,
   or should it remain invalidation-only?
2. Should launch requests with `initial_prompt` schedule title generation before
   provider output starts, assuming the prompt is already durable?
