# iOS Send Receipts And Activity Pulses

## Original Question

Dogfood iOS sends can feel opaque. A user can tap send, see an optimistic
bubble, and then be left guessing whether Longhouse is still submitting the
message, the Runtime Host accepted it, the durable transcript caught up, or the
agent has begun work.

The screenshot error copy, "The data couldn't be read because it is missing,"
was the first investigation gate. Hosted and local forensics changed the read:
the two screenshot messages were not lost. Both reached Codex, both were
accepted by the Runtime Host, both were recorded in `session_inputs` as
`delivered`, and both became durable transcript data. The bug class is therefore
not "send did not work"; it is **false-negative confirmation**: iOS let a
decode/load/refresh/presence-side error read as a send failure after the message
had already landed.

The requested product direction is:

- make the moment of send feel immediate and legible
- distinguish optimistic local rendering, server acknowledgement, transcript
  reconciliation, and agent execution
- avoid fake status animations that continue when no real data is moving
- explore a Longhouse-native "packet crackle" style animation driven by actual
  app-level transport/runtime events
- use current iOS and Liquid Glass interaction patterns without making the
  transcript noisy

This document scopes the work and orders it so backend/data truth comes before
SwiftUI polish.

## Product Goals

1. **Truthful send acknowledgement.** The user should know whether a message is
   local-only, in flight, accepted by Longhouse, queued, failed, or reconciled
   into the transcript.
2. **Visible liveness.** Once the agent is responding, the UI should show that
   events are arriving. This should be more expressive than a static "Working"
   label but still calm enough for a coding transcript.
3. **Data-coupled motion.** Animated energy should correspond to real
   Longhouse events: request start, response, SSE connect, SSE change,
   pubsub sequence advance, tail refresh, render beacon, provider runtime
   transition, reconnect, or error.
4. **No raw packet sniffing.** The product should not inspect literal network
   packets. iOS sandboxing, privacy, TLS noise, and App Store constraints make
   that the wrong layer. Longhouse should surface semantic packets it owns.
5. **Accessibility first.** Motion must respect Reduce Motion. Liquid/glass
   effects must remain legible under Reduce Transparency and Increased Contrast.

## Research Inputs

Exa research covered modern chat send microinteractions, Apple Liquid Glass
guidance, and prior art for realtime network activity visualization.

Notable findings:

- iMessage keeps send receipts tiny and sparse. `Delivered` appears under the
  latest relevant message, then later disappears to avoid polluting the thread.
- WhatsApp's 2026 iOS beta reintroduced message bubble animations with a fade
  and slight scale-in, but also added a chat animation setting because motion
  can bother users.
- Stream Chat's iOS docs separate `sent`, `delivered`, and `read` and tie
  richer states to WebSocket events. The important lesson is semantic precision:
  `sent` is not the same as recipient/device/read acknowledgement.
- Apple's Liquid Glass guidance favors controls that are quiet at rest and come
  alive on touch, with glow/flex under the finger. Custom effects should remain
  content-led and accessibility-aware.
- Packet/network visualization projects such as net-glimpse show packet events
  as blinking nodes/edges and fading trails. The useful pattern is event-driven
  energy decay, not literal packet capture inside the app.

## Existing Longhouse Ground Truth

Longhouse already has most of the ingredients:

- iOS generates a `clientRequestId` per send and sends it to
  `/api/sessions/{id}/input` or `/api/sessions/{id}/inputs-multipart`.
- iOS has `SubmittedInput` and `SubmittedInputPhase` for optimistic local
  bubbles.
- The POST returns `SessionInputResponse` with `outcome`, `inputId`,
  `liveInputId`, `clientRequestId`, `intent`, and queued rows.
- Backend `SessionInput.status` already represents durable input delivery:
  `queued -> delivering -> delivered | failed`, plus cancellation.
- Transcript events carry `inputOrigin.sessionInputId` and
  `inputOrigin.clientRequestId`, allowing iOS to reconcile optimistic bubbles
  against durable user rows.
- The iOS session detail listens to `/workspace/stream` over SSE and receives
  connected, changed, replay-gap, heartbeat, disconnected, and unauthorized
  events.
- Workspace changed events include `latest_event_id`, `server_fanout_at_ms`,
  `pubsub_seq`, and optional transcript preview data.
- iOS already posts client render beacons with emitted/rendered times,
  `client_received_at_ms`, and `pubsub_seq`.
- Server-side session turns already have timing nouns such as
  `send_accepted_at`, `active_phase_observed_at`, and `terminal_at`.

The primary gap is not lack of data. The gap is an explicit UI-facing contract
that turns these signals into honest user-visible states and event-driven
animation energy.

## 2026-07-07 Incident Findings

Forensics on the two dogfood screenshots:

- Bar follow-up session `4a52f6ef-3f4f-4c11-9ac0-26809c13b1c4`:
  `session_inputs` row `88` is `delivered`, with iOS client request id
  `ios-BBC82298-9A06-4C6D-8B60-C6F7818BBF1D`; `send_accepted_at` is
  `2026-07-06 20:08:14.995Z`, the provider reached terminal idle at
  `20:08:30.360Z`, and the turn became durable at `20:08:31.015Z`.
- Fresh iOS launch session `3e619cda-0af4-40cf-b09f-9b00a3622386`:
  hosted logs show `POST /api/sessions/launch` `200 OK`, then
  `POST /api/sessions/{id}/input` `200 OK`; `session_inputs` row `91` is
  `delivered`, with iOS client request id
  `ios-02DBD1B8-680F-4CCB-B183-85F22B5BDCB3`.
- During the fresh launch, `mobile-tail` and workspace stream requests returned
  `200 OK`, and live transcript publishes flowed continuously. Nearby
  `/api/agents/presence` calls returned transient `503`s while runtime/event
  ingest stayed healthy. Presence failures must not poison the send receipt UI.
- Current local `main` contains `1fcb6bbb5 Fix iOS send failure reconciliation`,
  but hosted build `ebae901f` does not contain it, and iOS does not auto-deploy.
  The phone likely reproduced stale or partially fixed behavior.

Updated root-cause framing:

> A message send has multiple confirmations. iOS must preserve the strongest
> confirmation it has already observed. Later failures in decode, mobile-tail,
> workspace stream, presence, or refresh may show a non-destructive "checking"
> or "refresh failed" state, but they must not downgrade an accepted/delivered
> send into "Send failed."

## Terms

- **Optimistic local render:** the user bubble inserted before Longhouse has
  accepted the input.
- **Server acknowledgement:** the input POST completed and the Runtime Host
  returned a structured `SessionInputResponse`.
- **Delivery:** the backend input lifecycle row reached `delivered`. Gate B must
  freeze whether this means provider/control-path acceptance or durable
  transcript proof before final user copy ships.
- **Transcript reconciliation:** a durable user event appeared with matching
  `session_input_id` or `client_request_id`.
- **Agent active:** runtime/transcript evidence shows the provider has begun the
  turn after the input.
- **Semantic packet:** a typed app-level event emitted by Longhouse code, not a
  raw TCP/IP packet.

## UI Contract

### User Bubble Receipt Ladder

The user-authored bubble should show sparse, deterministic states:

```text
tap
-> local optimistic bubble
-> Sending...
-> Sent | Queued | Checking... | Could not confirm
-> In transcript
-> receipt fades once the agent starts or a newer user message supersedes it
```

Recommended copy:

- `Sending...` for POST in flight
- `Sent` for Runtime Host acknowledgement / delivery response
- `Queued` when Longhouse accepted the message but will send at the next safe
  turn boundary
- `Checking...` when a transport error occurred but transcript reconciliation
  is still being checked
- `In transcript` only when durable reconciliation has happened
- `Could not confirm` when Longhouse might have accepted the message but iOS
  cannot currently prove it
- `Could not send` plus retry affordance only after explicit server rejection,
  local validation failure, or confirmed undelivered timeout

Avoid `Delivered` until the product defines exactly what recipient/device
delivery means for an agent session.

### Agent Liveness Surface

Agent liveness should not live inside the user bubble after delivery. Once the
agent begins work, attention should move to a small status surface in the
composer chrome or near the current assistant activity.

Animation directions considered:

1. **Signal Rail.** A compact glass rail/capsule where real events create short
   pulses. It is quiet at rest and decays quickly when no events arrive.
2. **Packet Crackle.** A tiny sparkline or detector-like strip where every
   semantic packet creates a tick. Loss of stream/connectivity should visibly
   flatten it.

Recommended sequence: build Signal Rail first. Keep Packet Crackle as a preview
experiment on the same derived signal after the rail proves the data coupling.
The Provider Reactor idea is intentionally deferred for launch.

## Architecture Review Synthesis

Independent review agreed with the broad order: truth before polish. It tightened the
plan in four important ways:

1. **Reframe this as a legibility projection, not a new subsystem.** Delivery
   truth, execution truth, and archive truth already exist. The iOS work should
   project those facts clearly instead of adding another source of truth.
2. **Reproduce the real bug first.** The "data missing" screenshot likely comes
   from `mobile-tail` decode or load handling, not the send POST. Fix that
   independent correctness bug before receipt animation.
3. **Freeze `delivered` semantics before copy.** If backend semantics still
   distinguish provider acceptance from durable transcript proof, `Sent` and
   `In transcript` must reflect the frozen contract.
4. **Shrink the animation layer.** Do not define a 20-plus event contract or run
   a three-way animation bake-off before launch. Build one derived signal from
   `SessionViewModel`, then one Signal Rail animation. Keep Packet Crackle as a
   preview experiment after the rail proves the data coupling.

First-principles framing:

> This is a client-side legibility projection problem. Map local POST state,
> `SessionInput.status`, `runtime_display`, and transcript reconciliation into
> sparse, honest receipts. Then let a small optional animation consume the same
> real events.

## Proposed Activity Pulse Model

Introduce a small derived pulse signal before building fancy animation views.
This should live on or be derived from `SessionViewModel`, which already
aggregates local send state, stream state, pubsub sequence, realtime telemetry,
and transcript items. It should not be a separate durable store or a second
source of "what is happening now."

Stable receipt-changing events:

```text
send_pressed
send_post_started
send_post_succeeded
send_post_failed
input_outcome_sent
input_outcome_queued
input_outcome_failed
transcript_reconciled
runtime_active_observed
```

Decoration-only pulse sources can remain open-ended and local:

```text
stream_connected
stream_changed
stream_replay_gap
stream_disconnected
pubsub_sequence_advanced
tail_response_received
render_beacon_posted
runtime_blocked_observed
runtime_terminal_observed
```

Heartbeats should either be excluded from the visual rail or rendered as a
distinct low-energy connectivity tick so they do not imply "the agent is
working."

If server support is needed later, add narrow fields or events to existing
contracts rather than a separate animation API.

Animation energy rules:

- user input events create small, fast pulses
- server acknowledgement creates a crisp receipt pulse
- transcript/render events create a visible activity tick
- heartbeats are excluded from the main rail by default; if shown, they must be
  visually distinct low-energy connectivity ticks
- errors/reconnects create a different pulse color/tone and can dampen normal
  animation until recovery
- no pulses means the animation decays to stillness

## Order Of Attack

### Gate A: Lock The False-Negative Class

- Convert the two incident findings above into a regression shape: a send can
  be accepted/delivered while a later load, decode, mobile-tail, workspace, or
  presence step fails.
- Add or extend focused iOS tests around the exact user-visible invariant:
  once `sendInput` returns a structured success, later refresh failure cannot
  mark that submitted input `failed`.
- Add a second test for ambiguous send confirmation: if the POST body cannot be
  decoded or the network drops after the request may have reached the server,
  the optimistic input moves to `checking` / `couldNotConfirm`, then reconciles
  by `clientRequestId` if tail/input state proves it landed.
- Ensure raw Swift messages such as "The data couldn't be read because it is
  missing" never appear in send or initial-session UI.
- Treat `/api/agents/presence` failures as runtime/connectivity degradation,
  not as send failure evidence.

Exit criteria:

- false-negative send failure is covered by tests
- accepted/delivered sends are never downgraded by later refresh errors
- user-facing copy distinguishes "could not send" from "could not confirm"

### Gate B: Delivery Contract Audit

- Trace the current iOS send path from tap to `SubmittedInput` to POST response
  to `refreshTail` to transcript reconciliation.
- Trace the server route for JSON and multipart input, including lifecycle
  transitions and idempotency by `client_request_id`.
- Confirm which server response fields are enough for iOS to distinguish
  accepted, queued, failed, and live/durable IDs.
- Freeze what `SessionInput.status == delivered` means for user copy:
  provider/control-path accepted, or durable transcript proven.
- Treat `runtime_active_observed` as a derived edge from `runtime_display`, not
  from turn timing fields unless a measured gap appears.
- Make `client_request_id` the durable reconciliation key for any ambiguous
  send. A successful response can use `inputId`/`liveInputId`; an uncertain
  response must still reconcile later by `clientRequestId`.
- Verify whether the existing `/inputs` route is enough to recover recently
  delivered inputs by `client_request_id`; if it only returns queued/failed
  rows, add a narrow receipt lookup or include delivered recent rows.

Exit criteria:

- one sentence defining what `Sent` asserts
- one sentence defining what `In transcript` asserts
- one sentence defining what `Could not confirm` asserts
- confirmation that existing `SessionInputResponse` plus receipt lookup fields
  are enough, or a precise API delta if not

### Gate C: Backend/API Readiness

Do this before UI animation only if Gate A or Gate B finds missing truth. The
incident evidence suggests the send data exists; the likely backend/API delta is
only a receipt-recovery helper, not a new delivery subsystem.

Possible work:

- shape input POST errors so iOS receives actionable structured messages
- expose missing lifecycle fields in `SessionInputResponse` only if needed
- emit or preserve enough `client_request_id` and `session_input_id` data for
  reconciliation
- expose a narrow "receipt by client request id" path if iOS cannot otherwise
  prove an ambiguous send landed
- keep presence `503` and WriteSerializer pressure out of the send failure
  response path unless they actually block the input POST
- add existing-workspace-stream invalidation for input lifecycle if the current
  stream cannot wake iOS quickly enough
- add tests for JSON empty-text rejection, multipart attachment-only sends,
  idempotent `client_request_id`, queued/delivering/delivered/failed states,
  and structured failure copy

Exit criteria:

- iOS can render honest send phases from API data without guessing
- failures have usable messages
- tests lock the lifecycle contract

### Gate D: iOS Receipt State Machine

- Extend `SubmittedInputPhase` or add a display-state adapter. iOS already has
  `submitting`, `sent`, `queued`, `failed`, and `needsUserDecision`; the likely
  additions are `checking` / `couldNotConfirm` and a short-lived `reconciled`
  display state.
- Keep `SessionInput.status` and transcript origin as the source of truth; the
  UI state should derive from them plus local request state.
- Surface the existing post-failure `refreshTail(allowFailure: true)` as
  `Checking...` instead of jumping immediately to a terminal-looking failure.
- Never show `Send failed` after a successful structured POST response.
- Never show `Send failed` for a post-send `mobile-tail`, workspace stream,
  timeline refresh, or presence failure.
- On initial fresh launch, keep a non-destructive "session started / waiting for
  first events" state if launch or input succeeded but the first transcript load
  is temporarily empty or fails to decode.
- Add a short-lived receipt under the optimistic/durable user bubble.
- Fade or remove receipts once the agent becomes active or a newer user message
  supersedes the status.
- Preserve the current de-duplication logic by matching `sessionInputId` or
  `clientRequestId`.

Exit criteria:

- tap gives immediate local feedback
- server ack changes visible state
- transcript reconciliation can be seen briefly
- duplicate optimistic/durable user rows do not appear
- failure states are specific and retryable

### Gate E: Derived Activity Pulse Signal

- Add a lightweight iOS-only derived pulse signal on `SessionViewModel`.
- Instrument existing send, tail, stream, pubsub, render-beacon, and runtime
  observation points.
- Build it so tests/previews can inject pulse sequences without real network.
- Respect Reduce Motion by reducing pulse motion to opacity/icon/text changes.

Exit criteria:

- there is one UI-facing pulse stream
- pulse stream is driven by real Longhouse events
- no animation view needs to inspect networking internals directly

### Gate F: Signal Rail Prototype

- Build Signal Rail in the composer/status chrome.
- Drive pulse intensity from `ActivityPulse` events and decay it over time.
- Keep it subtle under normal stream heartbeats.
- Make errors/reconnects visibly different without becoming alarming.
- Add SwiftUI previews/fixture scenarios for idle, sending, connected,
  active-crackle, reconnecting, failed, and Reduce Motion.

Exit criteria:

- user can tell data is moving
- animation dies quickly when events stop
- visual design remains quiet enough for transcript reading

### Later: Packet Crackle Preview Prototype

- Reuse the same activity bus.
- Prototype a detector/sparkline variant, likely hidden behind a local preview
  flag or fixture first.
- Compare against Signal Rail for legibility, calmness, and perceived trust.

Exit criteria:

- decision whether Packet Crackle becomes the shipping default, an alternate
  mode, or a discarded experiment

### Gate G: Verification

- Add focused iOS unit tests for receipt state derivation where practical.
- Add/extend fixture previews for send states and pulse scenarios.
- Run `make test-ios` for iOS code changes.
- Run backend tests (`make test` or focused `uv run pytest ...`) if server/API
  contracts change.
- Run preview rendering via `ios/scripts/render-previews.sh` for changed SwiftUI
  surfaces.
- Dogfood on the actual phone before calling receipt correctness or animation
  done. iOS does not ship on `git push`; build/install must be explicit.

## Open Decisions

1. Receipt text: `Sent` + `In transcript`, or `Sent` + `Agent received`.
2. Icon language: text only, checkmarks, phase dots, or text plus tiny icon.
3. First liveness animation: Signal Rail is the recommended first shipping
   direction; Packet Crackle remains a preview experiment.
4. Placement: composer chrome, under assistant bubble, or both.
5. Animation setting: rely on system Reduce Motion only, or add an app-level
   chat animation toggle.
6. Server changes: reuse existing fields only, or expose explicit receipt/timing
   fields in the input response.
7. Ambiguous-send recovery: use `mobile-tail` reconciliation only, or add a
   narrow receipt lookup by `client_request_id`.

## First Recommendation

Proceed in this order:

1. Lock the false-negative send failure class with tests and fixtures.
2. Audit/freeze receipt semantics: `Sent`, `Queued`, `Could not confirm`,
   `In transcript`, and `Could not send`.
3. Add a receipt-recovery API only if iOS cannot prove ambiguous sends by
   existing tail/input data.
4. Implement the iOS receipt state machine and non-destructive initial-load
   handling.
5. Verify on the physical phone with the current Xcode-installed build.
6. Add the derived activity pulse signal.
7. Build Signal Rail.
8. Packet Crackle preview prototype later.

Do not start with custom SwiftUI animation polish. The first shippable value is
an honest receipt ladder. The animation gets much better once it has real event
energy to consume.
