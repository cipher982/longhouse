# iOS Optimistic Session Input

Status: Proposed
Last updated: 2026-05-07

## Goal

Make iOS session control feel immediate and trustworthy when sending input to a
managed session.

The user action target is:

- tap send -> composer clears in the same interaction
- message appears locally as pending immediately
- network dispatch and workspace refresh reconcile in the background
- failures preserve or restore the user's text with an explicit retry path

This is part of the managed-local live-control launch loop. The mobile app must
not feel slower than a terminal just because the backend is verifying transport
acceptance or refreshing the full workspace.

## Problem

Today the iOS composer clears only after `SessionViewModel.send(...)` returns.
That method waits for `POST /api/sessions/{id}/input` and then performs a full
workspace refresh. `refreshWorkspace(...)` also awaits render telemetry. This
couples visible input state to network, provider dispatch, JSON decoding, and a
telemetry beacon.

Observed failure mode:

1. User taps send.
2. Text remains in the composer, making the tap look ignored.
3. The transcript may later show "thinking" from realtime refresh.
4. The original text can still remain in the composer, so the UI contradicts
   itself.

## Non-Goals

- Do not change backend queue or dispatch semantics.
- Do not make iOS silently remap explicit `steer` failures into queued input.
- Do not add a second transcript source of truth. Optimistic rows are local
  UI state and must disappear once the real transcript catches up.
- Do not add broad polling. Queue polling can be added only while visible
  pending/queued input exists.

## Client State Model

The composer owns transient draft text. The view model owns submitted input
state.

Each submitted input has:

- local id
- text
- intent: `auto | queue | steer`
- phase: `submitting | sent | queued | failed`
- optional server `input_id`
- optional `last_error`

State transitions:

```text
draft text
  -> submitting       tap send, clear composer immediately
  -> sent             POST returns outcome=sent
  -> queued           POST returns outcome=queued
  -> failed           POST fails, or structured steer turn_ended is rejected
  -> reconciled       transcript/workspace contains the durable user event
```

`failed` preserves the text in an explicit retry surface. It must not silently
repopulate the composer unless the user taps an edit/retry action.

## Send Path

Tap handling should be synchronous from the user's perspective:

1. Trim and capture the current composer text.
2. Clear the composer and resign focus immediately.
3. Insert a local pending input row.
4. Start the async `POST /input` task.
5. Apply the server outcome to local pending/queued/failed state.
6. Refresh the workspace in the background.

The send button can show an in-flight indicator, but the text field must not be
the in-flight indicator. Once the user taps send, the draft is no longer draft
state.

## Refresh And Telemetry Rules

- `POST /input` response handling must not await a full workspace refresh before
  returning control to the view.
- Workspace refresh should reconcile transcript state after the POST response or
  from SSE/polling.
- Render beacons are fire-and-forget. Telemetry must not block user-visible
  state, transcript refresh completion, or composer clearing.
- Existing backend `managed_turn_dispatch_seconds` and `dispatch_ms` remain the
  server-side send-accept truth. Client work should add or enable separate
  action timing later:
  - tap -> local clear
  - tap -> POST response
  - POST response -> transcript event rendered

## Queue And Failure Display

iOS already decodes `SessionInputResponse.queued`. For this phase:

- show queued/delivering count immediately from POST response
- show recent failed count immediately from POST response
- preserve the existing explicit `Queue instead` flow for `steer` +
  `turn_ended`
- do not hide failed queued rows behind a generic send error

Full web parity for queue chip list, cancel, and `/inputs` polling is a follow-up
unless the implementation stays small.

## Acceptance

- Tapping send clears the composer immediately, before `POST /input` completes.
- A local pending/sent/queued status appears without waiting for workspace
  refresh.
- If `POST /input` succeeds, the composer stays cleared.
- If `POST /input` fails, the submitted text remains available in a failed
  retry/edit surface.
- If workspace refresh fails after a successful POST, the submitted state still
  shows success/queued and the transcript is not blanked.
- Render telemetry cannot delay `refreshWorkspace(...)` completion.
- Unit tests cover immediate optimistic state, delayed refresh, failed POST, and
  nonblocking render beacon behavior.
