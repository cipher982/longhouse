# Session Continuation Lineage

Status: proposed
Owner: David / launch hardening
Updated: 2026-03-08

## Problem

Longhouse currently supports a Claude-only cloud continuation path, but it behaves like:

1. ship local transcript to Longhouse
2. export transcript from Longhouse
3. resume in cloud
4. ship cloud transcript back to Longhouse

This is good enough for one-way continuation, but it is not a true two-way synced transcript. If a user later keeps typing into the original laptop session, Longhouse can end up with divergent sibling sessions that are not modeled clearly in the product.

The product must not pretend this is Dropbox or Google Docs. We explicitly do not want transcript file sync in both directions.

## Product Principles

- One logical thread, many continuations.
- Exactly one writable head per thread.
- Older continuations are historical/stale, not secretly writable in place.
- First message from an older point creates a child continuation.
- Divergence is honest and visible; no hidden merge.
- Device/environment provenance is user-facing product state, not just debug metadata.

## User Model

Users think in terms of "this task", not raw provider transcript files.

Default timeline behavior:
- one card per logical thread
- card summary comes from the latest writable head
- card shows compact lineage hints like `Started on Cinder`, `Head: Cloud`, `2 continuations`

Detail page behavior:
- opening a thread lands on the latest writable head by default
- branch rail / continuation switcher shows other continuations
- viewing a stale continuation shows `This is not the latest continuation`
- latest head shows a normal active composer immediately
- stale continuation shows `Start new continuation from here`
- unsupported provider shows explicit unavailable state, not a fake composer

## Non-Goals

- No patching cloud changes back into provider-local laptop transcript files
- No automatic merge of divergent continuations
- No exposing internal terms like `commis` in the product UI
- No requirement that product lineage reuse the low-level `session_branches` ingest model

## Domain Model

Keep product lineage separate from rewind/source-line branch handling.

Add session-level lineage fields on `AgentSession` or an adjacent lightweight table:
- `thread_root_session_id`
- `continued_from_session_id`
- `continuation_kind` (`local`, `cloud`, later maybe `runner`)
- `origin_label` (`Cinder`, `Cloud`, `Cube`, etc.)
- `is_writable_head`
- optional `branched_at_event_id` or equivalent branch-point offset

The existing `session_branches` table remains for rewind-aware ingest/export. It is not the user-facing continuation graph.

## Core Rules

### Rule 1: Cloud continuation creates a child session

When a user sends the first web/mobile message from a synced session:
- create a new child continuation session
- mark it as the writable head
- preserve a pointer back to the source continuation
- do not present the original session as though it is being mutated in place

### Rule 2: Stale branches fork on write

If a viewed continuation is not the current writable head:
- do not write into it directly
- first user message creates a new child continuation from that point

### Rule 3: Later laptop shipping after cloud branch creates a local child

If the laptop continues shipping from an older branch after a cloud continuation already branched off:
- do not silently append to the pre-branch session row as if nothing happened
- create a new local child continuation instead

### Rule 4: One writable head

Within a logical thread:
- exactly one continuation is the writable head
- the timeline card opens this head by default
- old branches remain accessible but clearly historical

## Backend Ownership

### Models / session store

Own:
- lineage metadata
- writable-head selection
- stale branch detection
- child continuation creation rules

Likely files:
- `apps/zerg/backend/zerg/models/agents.py`
- `apps/zerg/backend/zerg/services/agents_store.py`

### Continuation service / session chat

Own:
- export snapshot for provider resume
- create child continuation on first cloud message
- lock web-vs-web access for the same writable head

Likely files:
- `apps/zerg/backend/zerg/services/session_continuity.py`
- `apps/zerg/backend/zerg/routers/session_chat.py`

### Shipper / ingest path

Own:
- local continuation ingest
- stale laptop divergence detection
- creating local child continuation rows after branch

Likely files:
- `apps/engine/src/pipeline/compressor.rs`
- `apps/zerg/backend/zerg/services/agents_store.py`

## Frontend Ownership

### Timeline

Render one card per logical thread, not one card per raw continuation session.

Requirements:
- show latest head summary
- show compact lineage metadata
- optionally expand to reveal continuations
- search/deep links may still target a specific continuation, but default thread navigation should open latest head

### Detail page

Requirements:
- latest head opens by default
- stale warning if not viewing head
- branch/continuation rail
- active composer only for head
- `Start new continuation from here` on stale continuations

Likely files:
- `apps/zerg/frontend-web/src/pages/SessionsPage.tsx`
- `apps/zerg/frontend-web/src/pages/SessionDetailPage.tsx`

## API / Query Shape

Need a thread-aware view model for UI. Two reasonable approaches:

Option A:
- keep session APIs as-is
- add thread projection endpoints for timeline/detail

Option B:
- extend existing session list/detail payloads with lineage/head metadata

Preferred first step:
- extend current session payloads enough to support one-card-per-thread rendering without introducing a full second API surface immediately

## Phased Implementation

### Phase 1: lineage metadata + head semantics
- add lineage fields
- add helper methods to identify thread root and writable head
- add backend tests for head selection and child creation

### Phase 2: explicit cloud child continuation
- on first cloud message, create child continuation session instead of mutating original
- keep Claude-only resume path for now
- update detail page to show head vs stale behavior

### Phase 3: thread-centric timeline
- render one card per logical thread
- open latest head by default
- show compact continuation count + origin/head labels

### Phase 4: laptop divergence handling
- detect later laptop shipping after branch
- create local child continuation instead of appending to stale pre-branch row
- add ingest regression tests

### Phase 5: provider parity
- extend continuation model to Codex/Gemini once provider-local reconstruction exists

## Testing Strategy

### Backend tests
- first cloud message from synced Claude session creates child continuation
- stale branch send creates new child continuation
- exactly one writable head exists after continuation creation
- later laptop ingest after cloud branch creates a local child continuation
- thread grouping / latest-head selection returns the expected row

### Frontend tests
- timeline shows one card for a threaded task with multiple continuations
- clicking thread opens latest head
- stale continuation shows warning + fork action text
- latest head shows active composer immediately
- unsupported provider shows explicit unavailable state

### Live QA
- create local Claude session on laptop
- ship to Longhouse
- continue once in cloud
- verify original is historical and cloud is head
- continue again locally from old laptop session
- verify new local child appears, not silent append into the original

## Acceptance Criteria

- Users see one task/thread card by default, not confusing duplicate top-level cards
- Cloud continuation is explicit and creates a child continuation
- Later local divergence is explicit and also creates a child continuation
- The UI makes latest head vs stale branch obvious
- No code path implies transcript file sync that does not actually exist
