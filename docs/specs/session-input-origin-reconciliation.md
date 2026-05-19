# Session Input Origin And Reconciliation

Status: Reviewed draft
Owner: Longhouse session kernel / iOS primary client
Updated: 2026-05-19

## Summary

Longhouse-authored user input needs first-class identity from tap/click through
provider delivery and transcript projection.

The transcript should show the human message and a semantic indication that it
was sent through Longhouse. It should not expose provider transport syntax such
as Claude `<channel>` tags. Optimistic UI should reconcile against durable
identity, not against raw text equality.

This is pre-launch. There are no external users, so favor the cleaner model now
over compatibility layers that would make the transcript contract harder to
reason about later.

## Problem

The iOS transcript work exposed a deeper issue:

1. A user sent a message from the iOS composer.
2. iOS rendered a local optimistic submitted-input bubble.
3. The provider transcript later contained a user event whose raw provider text
   was Claude channel transport syntax:

   ```text
   <channel ...>
   human message
   </channel>
   ```

4. Active tool calls rendered after that canonical user event.
5. The optimistic bubble briefly remained below those tool calls.

The duplicate was the symptom. The underlying problems are:

- Longhouse-authored input is stored separately from the provider transcript,
  but the eventual transcript event is not projected as the same logical user
  action.
- `SessionInput.request_id` currently does double duty as client idempotency key
  and dispatch/turn request id. Queue drain overwrites it with a drain request
  id, weakening idempotency and traceability.
- UI reconciliation falls back to raw text matching because workspace events do
  not carry a durable input identity.
- Provider transport details leak into user-facing transcript text.
- The UI has no semantic way to show "sent via Longhouse" versus "typed in the
  raw terminal."

## First Principles

- Raw provider evidence stays lossless. Source lines, raw JSON, and raw provider
  transcript text remain available for replay and debugging.
- Human-facing transcript projection is a product contract, not a provider raw
  dump.
- Longhouse-authored input is a durable product object with identity and
  lifecycle. It should not disappear into provider text once delivered.
- Optimistic UI rows are temporary client projections of `SessionInput`. They
  disappear only when the server proves the corresponding durable transcript
  event exists.
- Provider-specific transport syntax is a projection concern, not display text
  and not the semantic origin model.
- "Claude channel" is provider transport/debug metadata. "Longhouse" is product
  origin metadata.
- Pre-launch cleanup is preferred over legacy fallbacks. If old local dogfood
  data needs migration or discard, do that explicitly instead of preserving a
  vague runtime compatibility path.

## Current Model

Relevant existing tables:

- `session_inputs`
  - durable user-authored input
  - fields include `id`, `session_id`, `text`, `owner_id`, `intent`, `status`,
    `request_id`, `delivered_at`
- `session_turns`
  - canonical turn timing record
  - fields include `request_id`, `source_kind`, `user_event_id`,
    `durable_assistant_event_id`
- `events`
  - transcript event ledger
  - raw/source fields plus event projection fields
  - already has provisional event metadata

Useful existing behavior:

- Managed-local send verification can return `verified_user_event_id`.
- `session_turns.user_event_id` can already point at the durable user event.
- Server code already strips Claude channel wrappers in some turn/control
  verification paths.
- iOS workspace projection consumes `EventResponse` through
  `/api/timeline/sessions/{id}/workspace`.

Current gaps:

- `SessionInput` does not keep a stable client id separate from dispatch id.
- `SessionTurn` does not link back to the originating `SessionInput`.
- `EventResponse` does not expose input origin metadata.
- iOS/web cannot reconcile optimistic input by identity.

## Target Product Contract

When a user sends input through Longhouse:

1. The client creates a stable `client_request_id`.
2. The server creates or reuses one `SessionInput` for
   `(session_id, owner_id, client_request_id)`.
3. The input may be queued, delivering, delivered, failed, or cancelled.
4. When delivered to the provider and observed in the transcript, the server
   links the `SessionInput` to the `SessionTurn`; the turn already links to the
   durable user `AgentEvent` through `user_event_id`.
5. Workspace projection emits the user event with:
   - clean human display text in `content_text`
   - optional raw provider text when it differs
   - stable input identity
   - semantic origin metadata: sent via Longhouse
6. Native/web clients remove the optimistic row by identity.
7. The rendered user bubble shows a subtle Longhouse origin affordance without
   mentioning Claude channels.

When a user types directly in the provider terminal:

1. No `SessionInput` exists.
2. The durable `AgentEvent` has no Longhouse input link.
3. Workspace projection emits `authored_via = "terminal"`.
4. The UI renders a normal user bubble with no Longhouse badge.

Provider wrappers are display-normalized for both Longhouse-authored and
terminal-authored events. The origin badge comes from input linkage, not from
the wrapper.

## Idempotency Contract

`client_request_id` is a stable idempotency key minted by the client. Clients
should generate a UUID-backed value, optionally prefixed by surface
(`ios-...`, `web-...`). The server treats a collision for the same
`(session_id, owner_id, client_request_id)` as the same logical input.

Rules:

- Same session, owner, client id, same text:
  - `queued` / `delivering`: return the existing input state.
  - `delivered`: return the existing delivered state.
  - `failed`: retry the same `SessionInput` by assigning a new
    `delivery_request_id`; do not create a sibling row.
  - `cancelled`: return a structured conflict; the user must edit/send as a new
    input with a new client id.
- Same session, owner, client id, different text:
  - return structured conflict; never overwrite the original text.
- Same session and client id, different owner:
  - separate inputs; owner remains part of the unique key.
- Client id is never overwritten by queue drain, retry, steer, or managed-local
  delivery.

Conflict response shape:

```json
{
  "detail": {
    "error_code": "input_conflict",
    "existing_input_id": 44,
    "reason": "different_text"
  }
}
```

Allowed `reason` values:

- `different_text`
- `cancelled`

The unique index should be on `(session_id, owner_id, client_request_id)`, not
on dispatch request identity.

## Target Data Model

### SessionInput

Replace the overloaded `request_id` concept with distinct fields.

| Field | Purpose |
| --- | --- |
| `client_request_id` | Stable idempotency key minted by the client. Never overwritten. |
| `delivery_request_id` | Current dispatch/turn request id for locks, telemetry, and retries. |

Pre-launch cleanup rule:

- Rename/rebuild `request_id` into `client_request_id`.
- Add `delivery_request_id`.
- Do not keep a long-term `request_id` alias.
- Queue drain must set only `delivery_request_id`.

### SessionTurn

`session_turns` represents the provider turn and timing. It is the canonical
link between Longhouse input and durable transcript event.

| Field | Purpose |
| --- | --- |
| `session_input_id` | Nullable reference to `SessionInput.id`. |
| `request_id` | Dispatch/turn request id for timing and lock ownership. |
| `user_event_id` | Durable user transcript event for this turn. Already exists. |

Canonical chain:

```text
SessionInput -> SessionTurn -> AgentEvent(user_event_id)
```

Do not add `SessionInput.delivered_user_event_id` unless a measured query path
proves the one-join lookup is too expensive. Avoid dual-write pointer drift.

When send verification finds `verified_user_event_id`, update:

- `SessionTurn.session_input_id`
- `SessionTurn.user_event_id`

If a better proof later contradicts an existing link, do not silently rewrite
it. Treat that as an explicit reconciliation error path with a focused test.

### AgentEvent

`events.content_text` remains the raw extracted provider text in storage.

`EventResponse.content_text` is the user-facing display text for projection
routes. When raw provider text differs, `EventResponse.raw_content_text` may be
included for debug/machine consumers. Raw replay/export/source-line routes still
read from raw storage.

Do not persist projection-only fields on `events` until measured. The first
implementation should derive presentation from `AgentEvent`, `SessionTurn`, and
`SessionInput`.

## Provider Wrapper Handling

Keep the provider wrapper logic deliberately small.

Current recognized wrapper:

- full-message Claude `<channel ...>body</channel>`

Projection rule:

- If a full user message is exactly a recognized Claude channel wrapper,
  `EventResponse.content_text` is the wrapper body.
- `EventResponse.raw_content_text` is the original raw provider text when it
  differs.
- `input_origin.authored_via` does not come from the literal tag name. It comes
  from the `SessionInput -> SessionTurn -> AgentEvent` link.

Do not build a generalized provider-envelope service until a second provider
transport wrapper exists. A focused helper is enough. Do not put provider
envelope metadata in the primary client contract until a concrete UI or machine
consumer needs it.

## API Contract

`EventResponse` should make display text and input origin explicit.

Proposed Longhouse-authored event shape:

```json
{
  "id": 123,
  "role": "user",
  "content_text": "continue",
  "raw_content_text": "<channel ...>continue</channel>",
  "input_origin": {
    "authored_via": "longhouse",
    "session_input_id": 44,
    "client_request_id": "ios-..."
  }
}
```

Proposed terminal-authored event shape:

```json
{
  "id": 124,
  "role": "user",
  "content_text": "continue",
  "raw_content_text": null,
  "input_origin": {
    "authored_via": "terminal",
    "session_input_id": null,
    "client_request_id": null
  }
}
```

The server should not send display labels such as `"Longhouse"` as contract
fields. Clients render labels from `authored_via`.

Primary UI contract fields:

- `content_text`
- `raw_content_text`
- `input_origin.authored_via`
- `input_origin.session_input_id`
- `input_origin.client_request_id`

Debug/analytics fields such as `delivery_intent`, `delivery_status`, and
`delivered_via` should stay on session-input/turn routes unless a concrete UI
needs them.

## Client Rules

### iOS

- `SessionEvent` decodes `input_origin`, `raw_content_text`, and
  display `content_text`.
- User bubbles render `content_text`.
- User bubbles show a subtle Longhouse-origin marker when
  `input_origin.authored_via == "longhouse"`.
- Optimistic submitted rows reconcile by:
  1. `session_input_id`
  2. `client_request_id`
- Submitted rows do not reconcile by same raw text when identity is absent.
- The current iOS `ClaudeChannelText` display helper is deleted once the server
  projection contract is implemented.
- The WebKit transcript consumes the same semantic event/input model and is the
  single iOS transcript renderer.

### Web

- Timeline/session transcript user bubbles render `content_text`.
- Longhouse-authored events get the same semantic origin marker as iOS.
- Existing text-based optimistic reconciliation is replaced with identity-based
  reconciliation.

### Machine/API Consumers

- `/api/agents/*` routes returning `EventResponse` get the same projected event
  contract.
- Raw replay/export/source-line inspection keeps raw/lossless semantics.
- If a machine consumer needs raw provider text in normal event responses, it
  uses `raw_content_text` when present.

## Reconciliation Rules

Server-side:

- `POST /input` returns `input_id` and `client_request_id`.
- Dispatch/queue drain writes `SessionInput.delivery_request_id`.
- Dispatch verification writes `SessionTurn.session_input_id` and
  `SessionTurn.user_event_id`.
- Workspace projection finds Longhouse-authored user events by joining:

  ```text
  AgentEvent.id == SessionTurn.user_event_id
  SessionTurn.session_input_id == SessionInput.id
  ```

- A reconciler may attach an input to an event only when it has proof:
  - verified provider event id from managed-local send,
  - existing `SessionTurn.user_event_id` tied to the input,
  - future explicit provider request metadata if we add it.
- Raw text equality is not normal runtime proof.

Client-side:

- A submitted row with `serverInputId == event.input_origin.session_input_id`
  is removed.
- A submitted row with
  `clientRequestId == event.input_origin.client_request_id` is removed.
- A fixture event with identical text but no identity must not clear the
  optimistic row. This test guards against reintroducing text matching.

Branch/rewind rule:

- Clients reconcile only against events present in the current workspace
  projection. If a linked event is on an abandoned branch and not projected, it
  does not clear a visible optimistic row for the current head.
- The server must enforce this by emitting `input_origin` only on the projected
  event row currently visible to the client. A hidden/off-head linked event must
  not leak an origin identity into the current-head workspace projection.

## Testing Strategy

We need enough test coverage to replace "David notices while dogfooding."

### Backend Unit Tests

- Claude channel wrapper helper:
  - strips only full-message wrappers
  - preserves raw text when malformed or partial
  - handles bodies containing code or wrapper-like text conservatively
- Session input identity:
  - client request id is stable
  - duplicate `POST /input` with same client id/text returns same input
  - same client id/different text returns structured conflict
  - same client id from different owner creates a separate input
  - failed + same client id/text retries the same row with new
    `delivery_request_id`
  - cancelled + same client id/text returns structured conflict
  - queue drain does not overwrite client id
- Idempotency concurrency:
  - two simultaneous `POST /input` calls with same key create exactly one row and
    both responses reference it
- Send verification:
  - managed-local sent input links `SessionInput -> SessionTurn -> user_event`
  - steer sent input links the same chain
  - queued drain links the same chain
  - failed/turn-ended does not falsely link an event
- Session turn linkage:
  - `SessionTurn.session_input_id` is set for Longhouse-authored turns
  - reconstructed terminal turns remain unlinked

### Backend Integration/API Tests

- `POST /api/sessions/{id}/input` returns stable `input_id` and
  `client_request_id`.
- Workspace projection returns:
  - display `content_text`
  - raw `raw_content_text` when different
  - `input_origin.authored_via = longhouse`
  - `input_origin.session_input_id`
  - `input_origin.client_request_id`
- Workspace stream/SSE consumers see the same projected fields after refresh.
- Terminal-authored user event projects with clean display text and
  `authored_via = terminal`.
- Claude-wrapped Longhouse input projects clean display text plus Longhouse
  origin.
- Browser timeline workspace and agents workspace stay shape-compatible.
- Queue drain path is covered end-to-end:
  queued input -> drain -> provider user event -> workspace projection ->
  identity present.
- Branch/rewind fixture:
  linked event on abandoned branch does not reconcile current-head optimistic
  row.
- Provisional/durable fixture:
  active provisional events do not cause identity reconciliation unless the
  projection carries the proven input link.
- Provisional-to-durable transition fixture:
  a provisional event without identity is later replaced by a durable linked
  event, the workspace stream wakes clients, and optimistic rows clear only
  after the durable linked projection arrives.

### iOS Unit Tests

- Decode `raw_content_text` and `input_origin`.
- `TimelineBuilder` / `SessionViewModel` preserves origin metadata.
- Submitted input reconciles by `session_input_id`.
- Submitted input reconciles by `client_request_id`.
- Submitted input does not reconcile by same text when identity is absent.
- Native user bubble renders display text and origin marker.
- Web transcript payload uses display text and origin marker.
- Native and WebKit transcript payloads render the same display text from the
  same event fixture.

### iOS UI/Fixture Tests

- Fixture with one Longhouse-authored Claude-wrapped event:
  - only one user bubble renders
  - bubble text is clean
  - Longhouse origin marker is visible/accessibility-labeled
  - tool calls render after the canonical user event
  - no optimistic duplicate remains after workspace refresh
- Original regression fixture:
  iOS sends a message, optimistic row appears, a Claude-wrapped canonical user
  event arrives, active tool calls arrive after it, and the final state contains
  exactly one user bubble above the tool calls.
- Fixture with terminal-authored same text:
  - no Longhouse marker
  - no accidental reconciliation with a submitted row lacking identity

### Web Tests

- Component/unit test for event origin rendering.
- Optimistic send reconciliation test by input id/client id.
- Negative test: identical text without identity does not reconcile.
- Fixture or Playwright test for Longhouse marker and clean text.

### Migration/Schema Tests

- SQLite auto-migration renames/rebuilds `session_inputs.request_id` into
  `client_request_id` and adds `delivery_request_id`.
- Unique index becomes `(session_id, owner_id, client_request_id)`.
- Old local dogfood rows are either migrated explicitly or discarded explicitly;
  no permanent text-match compatibility path remains.

## Implementation Phases

### Phase 1: Display Projection Contract

- Server projects clean display text into `EventResponse.content_text`.
- Server includes `raw_content_text` only when it differs.
- Server leaves provider-envelope metadata out of the primary event contract.
- iOS and web can keep their current local stripping until their Phase 4/5
  cutovers consume the server projection everywhere. Double-stripping the same
  full-message wrapper is harmless during local dogfood, but the client helpers
  must be deleted in Phase 4/5.
- Add backend and client tests for clean display text.

This is not a temporary fallback. It is the correct projection contract and can
ship before identity migration.

### Phase 2: Backend Identity Model

- Rename/rebuild `SessionInput.request_id` into `client_request_id`.
- Add `SessionInput.delivery_request_id`.
- Add `SessionTurn.session_input_id`.
- Update creation, idempotency, queue drain, retry, conflict handling, and the
  concrete call sites that currently read/write `request_id`:
  `create_session_input`, `claim_next_queued`, `mark_delivered`,
  `mark_failed`, `_drain_next_queued_input`, and managed-local timing/logging
  paths in `session_chat_impl.py`.
- Add focused unit and concurrency tests.

This phase may ship the new column dark. Phase 3 turns on delivery writes and
origin projection.

### Phase 3: Delivery Linkage And Origin Projection

- Update managed-local send/steer/queue-drain paths to attach
  `SessionInput -> SessionTurn -> user_event` when verification succeeds.
- Use `verified_user_event_id` as primary proof.
- Add `input_origin` to `EventResponse`.
- Add backend API tests for Longhouse and terminal origin projection.

### Phase 4: iOS Client Reconciliation And Origin UI

- Decode and render `input_origin`.
- Replace text-based optimistic reconciliation with identity-based
  reconciliation.
- Render the Longhouse origin marker in the WebKit transcript.
- Delete the temporary iOS `ClaudeChannelText` helper and, specifically, the
  current `stripWrapper(event.contentText) == input.text` reconciliation path.
- Add iOS unit and fixture UI tests.

### Phase 5: Web Client Reconciliation And Origin UI

- Render display text and Longhouse marker.
- Replace optimistic reconciliation with identity-based reconciliation.
- Add component and fixture tests.

### Phase 6: Cleanup And Hardening

- Remove old request-id overloads and dead helper paths.
- Assert no runtime text-match reconciliation remains.
- Audit search/export/replay paths so raw evidence remains raw and user-facing
  snippets avoid transport markup where appropriate.
- Run backend, frontend, iOS, and fixture test suites.

## Acceptance Criteria

- A Longhouse-authored iOS send creates one visible user message after transcript
  catch-up, not an optimistic/canonical duplicate.
- The message displays human text, not Claude XML/channel syntax.
- The message carries a visible or accessible Longhouse origin indicator.
- Direct terminal-authored user messages do not carry the Longhouse indicator.
- Optimistic reconciliation is identity-based, not raw-text based.
- Queue drain preserves original client id and links the eventual transcript
  event back to the original `SessionInput` through `SessionTurn`.
- Duplicate POST/input behavior is explicit and covered by tests.
- Raw provider data remains available for replay/debug/export.
- Tests cover unit, concurrency, route integration, iOS view-model/UI fixture,
  and web rendering/reconciliation.
