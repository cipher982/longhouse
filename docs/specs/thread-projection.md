# Thread Projection

Status: proposed
Owner: David / continuity UX
Updated: 2026-03-19

## Executive Summary

Longhouse already models continuation lineage correctly enough in the database, but the primary detail view still behaves like a raw session inspector with a continuation dock bolted onto the bottom. That leaks implementation details into the product:

- users think they are opening one conversation and continuing it,
- the UI behaves like they are opening one segment and spawning more segments,
- the current page shows cloud/local seams as special-case chrome instead of transcript structure.

This spec makes the session detail page thread-native without inventing a new product surface. The route stays `/timeline/:sessionId`, but the center pane becomes a backend-owned projection of the selected session's lineage path. Users see one stitched timeline, inline seam markers, and one composer. Raw per-session event APIs remain available for audit/debug/MCP use.

## Problem

Today the data model already knows:

- which logical thread a session belongs to,
- which continuation is the current writable head,
- where a continuation branched from its parent.

But the detail page still renders only one concrete session at a time and asks the frontend to infer the "cloud continuation begins here" story. That causes three product problems:

1. The main pane breaks continuity at exactly the point where the product promise is "continue from anywhere."
2. The bottom continuation composer reads like a second chat surface instead of "reply in this thread."
3. Re-entering a head session can feel like it is branching again because the page centers session mechanics, not thread continuity.

## Product Principles

- One logical thread is the user-facing object.
- A session is one execution segment inside that thread.
- The main reading surface should follow one lineage path at a time, not expose the entire DAG at once.
- Seam markers are transcript structure, not floating helper UI.
- There is only one composer in the reading surface.
- Branching stays explicit when writing from a stale session, but audit detail remains available in the side rail and existing APIs.

## Decision Log

### Decision: Keep the existing detail route and add a projection endpoint

Context: `/timeline/:sessionId` is already the stable deep-link and the rest of the product points to it.

Choice: Keep the route stable and add a new browser/API payload for projection rather than introducing a brand-new thread page first.

Rationale: This improves the product mental model immediately without forcing routing churn or duplicate surfaces.

Revisit if: We later want `/timeline/threads/:threadId` as the canonical public URL.

### Decision: Project one lineage path, not every sibling branch interleaved

Context: A logical thread is a DAG, but flattening every sibling continuation into one scroll would be unreadable.

Choice: The projection for `/timeline/:sessionId` is the ordered lineage path from the thread root to the selected session. Sibling continuations stay discoverable in the rail.

Rationale: This matches how users read conversations. They want "show me how this branch got here," not "merge every fork into one transcript."

Revisit if: We later build a forensic graph view distinct from the main reading surface.

### Decision: Keep raw session event APIs for audit/debug/MCP

Context: Existing tooling, tests, and MCP workflows rely on session-scoped event access.

Choice: Add projection as a new API shape instead of mutating `/sessions/{id}/events`.

Rationale: Projection is product UX. Raw events remain the correct primitive for audit, export, and tooling.

Revisit if: Projection fully subsumes browser usage and we can simplify the old session-detail path.

### Decision: Server owns projection order and seam boundaries; client owns presentation copy

Context: The frontend should not keep reconstructing which sessions belong in the main path or where seams land.

Choice: The backend returns an ordered list of projection items plus seam metadata. The frontend turns seam metadata into copy/styles.

Rationale: Ordering and lineage truth belong on the server; copy and rendering stay flexible on the client.

Revisit if: Seam copy needs to be localized or shared across multiple clients.

## Domain Model

### Logical Objects

- **Thread**: the logical conversation anchored by `thread_root_session_id`
- **Segment**: one concrete execution session inside the thread
- **Focus session**: the concrete segment addressed by `/timeline/:sessionId`
- **Projection path**: the ordered ancestor chain from thread root to focus session
- **Seam**: the boundary inserted before a child segment begins in the projected transcript
- **Head**: the current writable segment for the thread

### Existing Fields We Reuse

- `thread_root_session_id`
- `continued_from_session_id`
- `continuation_kind`
- `origin_label`
- `branched_from_event_id`
- `is_writable_head`

No new persistence is required for the first version.

## UX Shape

### Sessions Page

No product reset here. The existing one-card-per-thread behavior stays.

### Detail Page

The detail page becomes:

- left rail: thread segments and metadata
- center pane: stitched projection for the selected lineage path
- right rail: inspector for the selected projected item
- bottom composer: the only place to send the next message

Behavior rules:

- Opening the thread head shows the full root-to-head path in one scroll.
- Each segment transition renders an inline seam marker in the transcript.
- Opening a stale segment shows the root-to-stale path, not the current head path.
- If the selected segment is the head, the composer replies on that head.
- If the selected segment is stale, the composer clearly branches from that point on first send.
- The old standalone continuation boundary banner/dock chrome goes away in favor of transcript seams.

## API Design

Add a browser-facing endpoint:

`GET /timeline/sessions/{session_id}/projection`

Query params for v1:

- `branch_mode=head|all` to control rewind-aware session internals per segment
- `limit`
- `offset`

Response shape:

- `root_session_id`
- `focus_session_id`
- `head_session_id`
- `path_session_ids`
- `total`
- `branch_mode`
- `abandoned_events`
- `items`

Projection item kinds:

- `event`
  - includes the existing event payload plus `session_id`
- `seam`
  - includes `session_id`
  - includes `continued_from_session_id`
  - includes `continuation_kind`
  - includes `origin_label`
  - includes `parent_origin_label`
  - includes `branched_from_event_id`
  - includes `timestamp`

Ordering rules:

1. Determine the projection path from root to focus session.
2. For each segment in order:
   - emit all visible events for that segment
   - except that for non-root segments, emit one seam item before the first event
3. `limit`/`offset` apply across the stitched item stream, not within a single segment.

Non-goals for v1:

- no interleaving of sibling continuations
- no text search/filtering on the projection API
- no new export contract

## Backend Design

Primary ownership:

- `apps/zerg/backend/zerg/services/agents_store.py`
- `apps/zerg/backend/zerg/routers/agents.py`
- `apps/zerg/backend/zerg/routers/timeline.py`

Required additions:

- helper to resolve the ordered lineage path for a focused session
- helper to count stitched projection items
- helper to fetch a paginated stitched slice across multiple segments
- response models for projection items and projection response

Important constraint:

- Keep `get_session_events()` untouched as the raw session primitive
- Build projection on top of existing session/event helpers

## Frontend Design

Primary ownership:

- `apps/zerg/frontend-web/src/services/api/agents.ts`
- `apps/zerg/frontend-web/src/hooks/useAgentSessions.ts`
- `apps/zerg/frontend-web/src/hooks/useSessionWorkspace.ts`
- `apps/zerg/frontend-web/src/lib/sessionWorkspace.ts`
- `apps/zerg/frontend-web/src/components/session-workspace/TimelinePane.tsx`
- `apps/zerg/frontend-web/src/pages/SessionDetailPage.tsx`

Required changes:

- fetch projection items instead of raw single-session events for the main center pane
- treat seam items as first-class timeline rows
- keep session/thread fetches for metadata and rail state
- keep one composer model based on selected segment head/stale semantics
- remove now-redundant ad-hoc continuation boundary logic

## Testing Strategy

### Backend unit coverage

- root session projection returns only root events and no seams
- child cloud session projection returns parent events, one seam, then child events
- stale local sibling projection follows the selected lineage path, not the current head sibling
- stitched pagination can cross a seam boundary correctly
- `branch_mode=head` keeps abandoned branch counts accurate across the stitched path

### Frontend unit coverage

- `useSessionWorkspace` consumes projection items and exposes seam rows in order
- seam rows render once at the actual boundary, not as a floating banner
- head sessions keep a reply composer; stale sessions stay branch-on-write
- returning to a head session does not duplicate the temporary compose scratchpad

### E2E coverage

- local/dev continuation flow shows one stitched timeline with one seam and one composer
- opening a stale branch shows the stale branch path rather than silently jumping to head
- resuming on the current head does not create duplicate inline blobs or duplicate seam chrome

### Live QA

- hosted `david010` still passes `make qa-live`
- hosted continuation/thread smoke asserts seam visibility and single-composer behavior on a real synced thread

## Implementation Phases

### Phase 0: Spec and task tracking

Acceptance criteria:

- this spec exists under `docs/specs/`
- one task file tracks active work
- `TODO.md` links to the task
- phase is committed before code changes

### Phase 1: Backend projection API

Acceptance criteria:

- browser/API endpoint returns stitched lineage-path items
- pagination works across seams and session boundaries
- raw session event APIs remain unchanged
- focused unit coverage exists for path selection and pagination

### Phase 2: Frontend projection consumer

Acceptance criteria:

- session detail main pane renders stitched items from the backend projection
- seam rows replace the ad-hoc continuation banner
- one composer remains at the bottom with correct head/stale behavior
- existing session rail and inspector continue to work

### Phase 3: Robust regression coverage

Acceptance criteria:

- backend tests cover projection edge cases
- frontend unit tests cover seam rendering and head/stale semantics
- core E2E covers stitched continuation behavior
- live continuation E2E covers the hosted path that previously regressed

### Phase 4: Ship and verify

Acceptance criteria:

- changes are merged to `main`
- runtime image build succeeds
- hosted instance is reprovisioned onto the new image
- local/unit/E2E and hosted QA all pass

## Verification Commands

- `make test`
- `make test-frontend-unit MINIMAL=1`
- `make test-e2e-single TEST=tests/core/session-continuity.spec.ts`
- `make test-e2e`
- `make qa-live`

## Acceptance Criteria

- Opening the current head of a continued thread shows one stitched transcript from the root segment through the current head.
- The exact local→cloud or cloud→cloud switch points render as inline seam rows in the transcript.
- Re-entering a head session does not show a second fake chat surface or duplicate scratchpad content.
- Opening a stale segment preserves that branch's own history path and makes branch-on-write explicit.
- The main UX speaks in thread continuity, while raw session-level inspection remains available for audit/debug paths.
