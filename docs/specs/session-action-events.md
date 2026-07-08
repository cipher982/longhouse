# Session Action Events

Status: Draft
Last updated: 2026-07-08
Owner: Longhouse

## Summary

Longhouse transcripts are not just user and assistant messages. Provider logs
also contain control and lifecycle artifacts: interrupted turns, cancelled
assistant messages, compaction markers, permission waits, recovery notices, and
runtime state changes.

The first user-visible failure is Codex interruption handling. Codex records an
intentional interrupt as structured provider evidence and also writes a
model-visible synthetic `<turn_aborted>...</turn_aborted>` user message. Today
Longhouse can ingest that synthetic marker as normal user text, so web and iOS
can render provider scaffolding as if the human typed it.

The fix is to add a provider-neutral **session action** concept. Interruption is
a session action, not a chat message.

## Goals

- Render intentional interrupts as compact action rows, not user messages.
- Keep raw provider evidence available for debug/export/replay.
- Share one server-side projection contract across web and iOS.
- Add CI coverage that would catch the screenshot bug before it ships again.
- Reuse the existing provider proof harness instead of creating a parallel test
  world.
- Start with Codex end-to-end, then map other providers only where structured
  evidence exists.

## Non-Goals

- Do not fork, patch, vendor, or pin provider CLIs.
- Do not infer interruption from assistant prose or generic errors.
- Do not make clients parse provider-specific strings.
- Do not add a durable `session_actions` table in the first slice unless the
  read-time projection approach proves insufficient.
- Do not solve live assistant preview ordering in this spec. That is a
  companion issue because it sits in workspace streaming and pending-input
  ordering, not in interruption classification.

## Current Behavior

Codex may emit both rows below for one user interrupt:

```json
{"type":"event_msg","payload":{"type":"turn_aborted","reason":"interrupted"}}
{"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"<turn_aborted>\nThe user interrupted...\n</turn_aborted>"}]}}
```

The first row is structured provider/runtime evidence. The second row is a
model-visible marker. It is useful to Codex, but it is not human-authored
conversation text.

Important current-state detail: the Rust parser drops Codex `event_msg` rows
before durable ingest. That means marker-only classification is not a rare
legacy fallback. It is the current steady state for already-ingested Codex
interruptions, and it remains the only signal until the parser ships an
allowlisted `event_msg.turn_aborted` action.

## Product Contract

User-facing timelines should render an interrupt as:

```text
User interrupted the turn
```

Normal timeline, mobile tail, search, title, summaries, recall, and clean
transcript exports must not treat `<turn_aborted>` as human-authored text.

Raw export and forensic/debug views may expose the original provider lines.

## Projection Contract

Projection items should be typed. A session action is a sibling of an event or
seam item, not an overloaded event role.

```json
{
  "kind": "action",
  "session_id": "session_123",
  "timestamp": "2026-07-08T21:00:00Z",
  "action": {
    "id": "event-or-source-id",
    "kind": "turn_interrupted",
    "provider": "codex",
    "source": "user",
    "provider_reason": "interrupted"
  }
}
```

The first slice should ship one action kind:

- `turn_interrupted`

Provider nuance belongs in `source`, `provider`, and `provider_reason` until a
second user-facing rendering exists. Clients map `kind` to display copy; the API
should not require English `label` as a stable contract field.

## Storage Strategy

First slice: use read-time action projection over existing event/raw source
identity.

Do not introduce a durable `session_actions` table yet. A new table would force
replay/dedup semantics, duplicate raw source coordinates, and add a migration
before the product contract is proven. Add durable action rows later only if
projection cost, query complexity, or product requirements justify it.

Parser-level minimum:

- Allowlist Codex `event_msg.payload.type = "turn_aborted"` only.
- Continue dropping other `event_msg` noise such as token counts.
- Suppress or hide the synthetic marker from normal message-visible views.
- Preserve raw source lines for export/debug.

## Provider Mapping

### Codex

- Parse allowlisted `event_msg.payload.type = "turn_aborted"` with reason
  `interrupted` as a `turn_interrupted` action source.
- Treat `<turn_aborted>...</turn_aborted>` marker-only rows as the current
  compatibility path for already-ingested sessions.
- Pair typed and synthetic rows by source order/offset when both are available:
  a marker immediately following a typed `turn_aborted` row in the same Codex
  rollout is the synthetic marker for that action.
- Suppress the paired synthetic marker from normal message projection.
- Keep the marker parser provider-specific and server-side. Clients must not
  parse `<turn_aborted>` strings.

### OpenCode

- Map structured abort evidence such as `MessageAbortedError` and session abort
  outcomes to `turn_interrupted`.
- Do not render zero-part aborted assistant messages as blank assistant rows.
- Defer implementation until the Codex slice proves the shared action contract.

### Claude

- Map explicit Longhouse-controlled interrupts to actions at the control layer.
- Do not infer actions from unmanaged Claude transcript prose.
- Defer implementation until the Codex slice proves the shared action contract.

### Cursor

- Map structured ACP cancelled/stop outcomes when available.
- Do not classify generic errors as user interrupts.

### Antigravity

- Keep interruption unsupported until there is stable structured provider
  evidence or a Longhouse-owned control path.

## UX Rules

- Timeline action rows are compact and visually quieter than messages.
- Action rows are neither left/right chat bubbles nor assistant prose.
- Web and iOS use the same projection item shape.
- Clients own local copy for known action kinds.
- Unknown action kinds should degrade gracefully to a compact lifecycle row.
- The first implementation should prioritize clarity over decoration.

## CI And Integration Strategy

Existing provider canaries prove part of the surface: Longhouse can dispatch an
interrupt and observe a terminal `interrupted` or `cancelled` provider state.
That protects the control path. It does not protect the user-facing transcript
contract.

Add always-on CI gates at four layers:

1. Rust parser goldens:
   - Codex typed `turn_aborted` plus paired synthetic marker becomes one action
     and zero human user messages.
   - Codex marker-only rows become one action with
     `provider_reason = "marker_only"`.
   - Non-allowlisted `event_msg` rows are still dropped.
   - Existing context-injection filtering still works.
2. Backend projection tests:
   - Workspace projection and mobile tail expose an action row and not marker
     text.
   - Message-visible helper paths used by title, search, summaries, recall, and
     clean transcript exclude marker text.
   - Raw export/debug paths still include provider evidence.
3. Client shared fixtures:
   - The server owns a checked-in projection fixture under
     `tests/fixtures/session-projection/`.
   - Web Vitest and iOS XCTest both load that same JSON fixture.
   - Regeneration or fixture updates must be explicit and reviewable.
   - Web and iOS render `kind = "action"` and hide raw marker text in normal
     timelines.
4. Provider proof harness:
   - Provider update canaries should assert action projection after a real or
     hermetic interrupt scenario.
   - Artifacts should show raw provider deltas when provider interruption shapes
     change.

This is the general Longhouse integration-test pattern we want more of:

```text
real/fixture provider evidence
  -> parser/canonical events
  -> Runtime Host DB ingest
  -> API projection
  -> web/iOS shared fixtures
  -> provider proof artifact
```

## Implementation Phases

### Phase 1: Codex Parser And Safety Floor

- Extend parser snapshot shape with stable `kind` / `action_kind` fields.
- Teach parser to allowlist Codex `event_msg.turn_aborted`.
- Reclassify/suppress synthetic `<turn_aborted>` markers from normal user
  messages.
- Add parser goldens for typed, marker-only, paired, and noise-still-dropped
  cases.
- Make the screenshot bug disappear even before action-row UI lands by ensuring
  marker text is hidden from normal message-visible rows.

### Phase 2: Backend Projection Contract

- Add read-time action projection model/service.
- Expose `kind = "action"` through session projection and mobile tail.
- Add a helper for message-visible transcript text and route title/search/
  summary/clean transcript consumers through it where practical.
- Add backend tests using marker-only and typed Codex fixtures.

### Phase 3: Web And iOS Action Rows

- Add action-row rendering to web timeline/session workspace.
- Add action-row rendering to iOS timeline/transcript surfaces.
- Add one shared projection fixture consumed by both clients.

### Phase 4: Provider Harness Hardening

- Extend universal harness interrupt scenarios to assert projection semantics.
- Add OpenCode aborted-message fixture.
- Add Claude control-layer interrupt fixture.
- Keep Antigravity as an explicit unsupported gap.

### Phase 5: Companion Preview-Ordering Fix

- Handle live assistant preview ordering in a separate focused spec/patch.
- That work should cover pending user input before assistant preview and
  triggering-input linkage in workspace streams.

## First Slice Acceptance Criteria

- The Codex screenshot bug is impossible in normal web/iOS timeline rendering.
- Typed and marker-only Codex interruption fixtures are covered in CI.
- Existing provider interrupt control canaries remain green.
- Web and iOS consume a shared action projection fixture.
- Raw provider evidence remains available for debug/export.
