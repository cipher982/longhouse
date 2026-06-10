# Transient Transcript Placeholder UX

Status: planned

## Problem

Managed sessions can briefly report that the provider turn ended before the
durable assistant transcript is visible to clients. The runtime display layer
currently exposes that internal handoff as user-facing copy: "Response ready",
"Updating transcript", or earlier "Syncing". That is a product bug. It makes a
normal sub-second to multi-second materialization gap look like an explicit sync
workflow.

The internal state is still useful. `syncing_transcript` prevents the UI from
flashing to idle after a prompt when the assistant response is not yet renderable.
The bug is the presentation mapping, not the runtime detection.

## UX Contract

1. `syncing_transcript` remains an internal runtime state.
2. No happy-path surface may show copy containing "sync", "transcript",
   "response ready", or "updating" for this state.
3. While the gap is normal, clients continue the existing active response affordance:
   a working status, animated presence, and the assistant placeholder/spinner in
   the chat transcript.
4. Completion is content-visible, not lifecycle-visible. The UI should look done
   only when the assistant text is renderable or the provider is waiting for user
   input.
5. Slow or failed transcript materialization may have explicit copy, but it must
   be scoped to the chat placeholder and use product language such as "Loading
   response" or "Couldn't load this response." That failure UX is out of scope for
   this pass unless the backend already exposes a durable failure signal.

## Surface Behavior

### Runtime Display API

When `build_session_runtime_display()` detects `syncing_transcript`:

- Keep `state = "syncing_transcript"` so clients can distinguish the internal seam.
- Keep `tone = "active"`.
- Keep `is_idle = false`, `is_executing = false`, and `needs_attention = false`.
- Project safe copy:
  - `headline = "Working"`
  - `detail = null`
  - `phase_label = "Working"`

This keeps direct renderers like the web runtime strip and iOS runtime dock from
leaking implementation language.

### Timeline Cards

Timeline card status for `syncing_transcript` maps to:

- `label = "Working"`
- `tone = "thinking"`
- `seen_at = presence_at`
- `seen_at_prefix = "Updated"`

The row should read like the existing active state, not like a separate sync or
completion event.

### Web Chat

For managed-local SSE completion with `sync_status: "pending"` and no assistant
text yet:

- Leave the assistant message content empty.
- Keep `isStreaming = true` so the existing spinner bubble remains visible.
- Refresh/invalidate session data as today.
- Clear the optimistic streaming placeholder only after durable events refresh
  into the workspace or the send is explicitly failed/cancelled.

Do not insert text like "Response returned" or "Updating transcript" into the
assistant message.

### Presence Badge

`syncing_transcript` renders as the same visual class as an active response:

- compact title: "Working"
- full label: "Working"
- dot animation/color follows the existing thinking/working treatment

It must not render "Syncing", "Updating transcript", or a special purple
sync-only treatment.

### iOS

iOS should continue to trust the server runtime display fields. Tests and any
fixtures must expect the safe projection above. Because iOS detail, widget, and
Live Activity surfaces render the server `headline`, `detail`, and
`phase_label`, the backend projection change covers those surfaces with no iOS
view edits. Only fixtures and test expectations should change unless a local
fallback path is found.

### Desktop Menu Bar And Snapshot Tooling

The desktop menu bar uses `server/zerg/config/managed_phase_contract.json`, not
the runtime-display projection, to decide whether a managed phase is known. Add
`syncing_transcript` to that contract with `attention = "working"` so the menu
bar does not raise an unknown-phase health warning during this normal handoff.
Regenerate the Swift contract with
`scripts/generate_managed_phase_contract_swift.py`.

The widget snapshot helper has local fallback phase logic for screenshots. It
should also treat `syncing_transcript` as active/working so local visual checks
do not render the state as inactive.

## Implementation Plan

1. Backend projection:
   - Update `session_runtime_display.py` copy for transcript-sync pending.
   - Update `session_views.py` timeline-card mapping.
   - Update focused backend tests for runtime display, freshness contract, and
     timeline overlay.
2. Web presentation:
   - Update `PresenceBadge.tsx` to collapse `syncing_transcript` into the working
     visual treatment.
   - Update `SessionChat.tsx` pending-sync SSE handling to retain the spinner
     placeholder instead of replacing it with copy.
   - Add or update component tests covering both cases.
3. iOS model tests:
   - Update decoded runtime-display expectations to match the server projection.
   - Add a targeted assertion that no internal transcript-sync copy appears in
     the fixture.
4. Desktop/menu-bar contract and fixture tooling:
   - Add `syncing_transcript` to `managed_phase_contract.json` as working.
   - Regenerate `ManagedPhaseContract.generated.swift`.
   - Update local-health/menu-bar contract tests.
   - Update widget snapshot fallback logic if it maps raw presence locally.
5. Validation:
   - Backend: `make test`
   - Frontend: `make test-frontend`
   - iOS: `make test-ios`
   - Relevant E2E/UI: run the session/timeline smoke that exercises timeline rows
     and session detail, or the closest available E2E target if no dedicated test
     exists.

## Acceptance Criteria

- Searching product source and tests for the old happy-path strings finds no
  user-facing runtime copy:
  - "Syncing"
  - "Syncing transcript"
  - "Response ready"
  - "Updating transcript"
  - "Response returned. Updating transcript"
- `syncing_transcript` remains in API enums and internal tests.
- Timeline rows and detail strips show generic active/working state during the
  transient handoff.
- Chat shows a non-textual assistant placeholder during pending transcript
  materialization.
- The menu bar does not raise an unknown-managed-phase warning for
  `syncing_transcript`.
- The widget snapshot fallback does not render `syncing_transcript` as inactive.
- Backend, frontend, iOS, and relevant E2E checks pass or have a documented
  unrelated failure.
