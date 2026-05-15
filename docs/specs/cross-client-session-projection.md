# Cross-Client Session Projection

Status: In progress
Owner: launch quality
Updated: 2026-05-15

## Goal

Keep web native to React and iOS native to SwiftUI while preventing the two
clients from disagreeing about what a Longhouse session means.

The server owns semantic projections: runtime state, lifecycle, capability
gating, timeline card status, and freshness. Clients own layout, interaction,
accessibility, and platform-specific grouping where the product deliberately
differs.

## Non-Goals

- Do not make the server return fully rendered timeline rows.
- Do not move client layout policy into the backend.
- Do not replace the native iOS app with a PWA, Electron shell, or web view.
- Do not introduce a new schema system while OpenAPI and JSON fixtures are
  enough.

## Problem

Web and iOS both render timeline/session/chat surfaces. They currently duplicate
some projection logic:

- tool-call pairing and orphan handling
- dropped vs running unresolved tool calls
- managed vs unmanaged labels
- runtime phase and freshness display
- composer enablement and disabled reasons

Small drift here makes Longhouse look unreliable: web can show a completed tool
while iOS shows a running one, or iOS can keep a stale phase alive after web has
retracted it.

## Target Contract

1. Backend APIs expose semantic truth using fields such as `runtime_display`,
   `runtime_facts`, `timeline_card`, and `capabilities`.
2. Clients consume those fields directly when present.
3. Client fallbacks exist only for old or incomplete payloads and are covered by
   tests.
4. Shared fixtures describe session payloads and expected semantic projection
   results.
5. Web and iOS run tests against the same fixtures.

## Phases

### Phase 1 - Shared Fixture Contract

Add checked-in JSON fixtures under `tests/fixtures/session-projection/`.

Each fixture should contain:

- `name`
- `projection.items`, shaped like `/api/agents/sessions/{id}/workspace`
- `expectations`, limited to semantic facts both clients should agree on

Initial expectations:

- row kind sequence
- tool pairing mode
- call/result ids
- orphan tool ids
- collapsed group sizes where the policy is shared

Done criteria:

- Web test loads at least one shared fixture.
- iOS test loads the same fixture.
- A known client divergence is fixed or explicitly recorded.

### Phase 2 - Semantic Runtime Fixtures

Extend fixtures beyond tool pairing:

- managed fresh phase
- managed stale phase
- unmanaged process-visible
- unmanaged transcript-only recent
- closed lifecycle
- missing legacy fields

Done criteria:

- Web and iOS agree on label/tone/freshness semantics for the same fixture.
- Client fallback behavior is visible in fixture tests rather than hidden in
  ad hoc test helpers.

### Phase 3 - Generated iOS API Types

Generate iOS API models from the backend OpenAPI contract.

Done criteria:

- New server fields appear in Swift through generation, not hand-written mirror
  edits.
- Hand-written `SessionModels.swift` types are reduced to extensions and UI
  helpers where practical.
- Generated files live in an obvious generated-code path and are not edited
  manually.

### Phase 4 - Delete Duplicate Client Derivations

Collapse state machines that the backend already owns.

Candidates:

- runtime status label/tone derivation
- compact tool label derivation
- dropped tool semantic flags if promoted server-side
- composer enablement and disabled reason

Done criteria:

- Clients render server semantic fields first.
- Compatibility fallbacks are isolated, named, and tested.
- Web and iOS have matching behavior on the shared fixture set.

### Phase 5 - Cross-Client CI Gate

Make drift a CI failure.

Done criteria:

- Fixture tests run in normal web and iOS test commands.
- Golden updates are reviewable diffs.
- A fixture addition is required for new timeline/runtime edge cases.

## Server-Owned vs Client-Owned

Server-owned:

- lifecycle
- control path
- capability gating
- semantic phase, tone, and freshness
- timestamps and freshness deadlines
- stable IDs and event identity

Client-owned:

- native layout
- row density
- small-screen grouping
- keyboard and scroll behavior
- selection/focus state
- accessibility presentation

## First Build Slice

Start with tool-call pairing because it is deterministic, small, and already has
known cross-client drift:

- Web supports FIFO result pairing for tool results without `tool_call_id`.
- iOS currently pairs only by `tool_call_id`.

The first shared fixture should fail without the iOS FIFO fix and pass after it.
