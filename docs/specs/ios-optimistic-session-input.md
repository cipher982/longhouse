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
- client request id
- text
- intent: `auto | queue | steer`
- phase: `submitting | sent | queued | failed | needs_user_decision`
- optional server `input_id`
- optional `last_error`

State transitions:

```text
draft text
  -> submitting       tap send, clear composer immediately
  -> sent             POST returns outcome=sent
  -> queued           POST returns outcome=queued
  -> needs_user_decision
                      explicit steer races terminal; user may queue instead
  -> failed           POST fails or submit timeout expires
  -> removed          transcript/workspace contains the durable user event
```

`removed` is not a visible phase. It means the optimistic row was reconciled and
removed from the pending collection.

`failed` preserves the text in an explicit retry surface. It must not silently
repopulate the composer unless the user taps an edit/retry action. If the user
has typed a new draft before the failure arrives, the failed text stays in the
retry surface and never overwrites the active composer.

`needs_user_decision` is not delivery failure. It is the existing `steer` +
`turn_ended` contract where the user explicitly chooses whether to queue the
text for the next turn.

## Idempotency And Reconciliation

The client must mint a stable client request id per submitted input. Retries of
that submitted input reuse the same id. The backend should treat that id as an
idempotency key for the target session and author, using the existing durable
input/turn record where possible instead of creating a duplicate send.

Optimistic rows reconcile in this order:

1. Prefer a durable transcript user event that carries the same client request
   id or server input id.
2. Until transcript events carry that id end-to-end, fall back to a bounded
   match on author, exact text, and a short timestamp window after submit.
3. If no match appears before the reconciliation deadline, keep the row visible
   as `sent` or `queued` rather than duplicating it in the transcript.

The implementation must not render both an optimistic row and a matching durable
transcript row indefinitely.

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

`submitting` has a bounded timeout. If the app backgrounds, loses network, or
the request hangs past that timeout, the row becomes `failed` with retry/edit
available on resume. This phase does not introduce offline local queueing.

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
- never overwrite a newer active draft with failed submitted text

Full web parity for queue chip list, cancel, and `/inputs` polling is a follow-up
unless the implementation stays small.

## Acceptance

- Tapping send clears the composer immediately, before `POST /input` completes.
- A local pending/sent/queued status appears without waiting for workspace
  refresh.
- If `POST /input` succeeds, the composer stays cleared.
- If `POST /input` fails or times out, the submitted text remains available in a
  failed retry/edit surface and does not overwrite newer draft text.
- If workspace refresh fails after a successful POST, the submitted state still
  shows success/queued and the transcript is not blanked.
- Render telemetry cannot delay `refreshWorkspace(...)` completion.
- Explicit `steer` + `turn_ended` shows the existing `Queue instead` decision
  surface, not a generic failure.
- Retrying a submitted input uses the same client request id, so a lost response
  cannot double-send if the backend already accepted the original request.
- Reconciliation removes a matching optimistic row when the durable transcript
  event appears, or leaves a single optimistic row visible if the transcript has
  not caught up.
- Unit tests cover immediate optimistic state, delayed refresh, failed POST, and
  nonblocking render beacon behavior.
