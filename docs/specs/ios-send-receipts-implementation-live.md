# iOS Send Receipts Implementation Live

Live implementation ledger for the first iOS send-receipt epic. Keep this file
short and current so a future agent can resume after compaction without
reconstructing the incident.

Companion spec: `docs/specs/ios-send-receipts-and-activity-pulses.md`

## Goal

Finish the send-receipt correctness epic up to the point where David can install
the iOS app with Xcode and dogfood it on the phone.

Pause after that dogfood handoff and recap before starting the next epic:
activity pulses / Signal Rail / Packet Crackle.

## Success Criteria

- iOS no longer shows `Send failed` after the server accepted or delivered the
  message.
- Raw Swift decode/load text such as `The data couldn't be read because it is
  missing` is not surfaced in send or initial-session UI.
- The UI distinguishes `Sending`, `Sent`, `Queued`, `Could not confirm`,
  `In transcript`, and true `Could not send`.
- Later `mobile-tail`, workspace stream, timeline refresh, or presence failures
  cannot downgrade a known accepted/delivered send.
- Ambiguous sends can reconcile by `clientRequestId` when later tail/input data
  proves the message landed.
- Focused iOS tests cover false-negative send failures and ambiguous
  confirmation.
- Any backend/API delta is narrow, tested, and only added if existing data is
  insufficient.
- `make test-ios` passes, or any failure is understood and documented.
- David is told exactly what to install via Xcode and what dogfood scenario to
  try.

## Current Evidence

- Screenshot 1 session: `4a52f6ef-3f4f-4c11-9ac0-26809c13b1c4`.
  `session_inputs` row `88` is `delivered`; `send_accepted_at` was
  `2026-07-06 20:08:14.995Z`; durable at `20:08:31.015Z`.
- Screenshot 2 session: `3e619cda-0af4-40cf-b09f-9b00a3622386`.
  Launch and input POST both returned `200 OK`; `session_inputs` row `91` is
  `delivered`.
- Nearby `/api/agents/presence` returned transient `503`s during screenshot 2,
  while input, tail, workspace stream, runtime ingest, and transcript publish
  succeeded.
- Local `main` includes `1fcb6bbb5 Fix iOS send failure reconciliation`.
  Hosted `david010` was on `ebae901f` during the investigation and did not have
  that commit. iOS does not auto-deploy from push.

## Stage Checklist

### Stage 0: Evidence And Spec

- [x] Find both screenshot messages locally and on hosted.
- [x] Confirm both messages reached Codex and became durable.
- [x] Reframe root cause as false-negative confirmation.
- [x] Update companion spec with incident findings and revised order.
- [x] Create this live implementation ledger.

### Stage 1: Contract Audit

- [x] Read current iOS send path: optimistic bubble, `sendInput`, decode,
  `refreshTail`, and reconciliation.
- [x] Read current server JSON and multipart input routes.
- [x] Define exact copy semantics:
  `Sent`, `Queued`, `Could not confirm`, `In transcript`, `Could not send`.
- [x] Decide whether existing `mobile-tail` plus queued-input routes can prove
  ambiguous sends by `clientRequestId`.
- [x] Document that no receipt-lookup API delta is needed for this dogfood
  slice.

Stage 1 decision: no backend/API delta for the first Xcode dogfood slice.
The server already records idempotent `client_request_id` receipts and replay
handles duplicate POSTs. The iOS proof boundary for "In transcript" is the
head-branch durable user event carrying the same `clientRequestId`. The
existing queued-input route is intentionally active-queue/failure state, not
delivered-proof state; add a delivered receipt lookup only if dogfood shows tail
projection lag is still the user-visible gap.

### Stage 2: Tests First

- [x] Add iOS test: successful send response followed by refresh failure does
  not mark the submitted input failed.
- [x] Add iOS test: ambiguous send confirmation moves to checking /
  could-not-confirm instead of terminal failure.
- [x] Add iOS test: later transcript/input reconciliation by `clientRequestId`
  clears the ambiguous state.
- [x] Skip backend tests because no receipt lookup/API change is needed.

### Stage 3: Implementation

- [x] Add or refine `SubmittedInputPhase` / display-state adapter.
- [x] Preserve strongest-known confirmation for each submitted input.
- [x] Make post-send refresh failures non-destructive.
- [x] Make initial fresh-launch load failures non-destructive when launch or
  input succeeded.
- [x] Remove raw decode/load copy from user-facing send/session messages.
- [x] Skip backend/API support because Stage 1 did not prove it is needed.

### Stage 4: Verification

- [x] Cover the focused iOS cases in the supported `make test-ios` harness.
- [x] Run `make test-ios`.
- [x] Render SwiftUI previews if receipt UI surfaces changed.
- [x] Run backend focused tests if backend/API changed.
- [x] Check `git status` and commit only files touched for this epic.

Verification note: `make test-ios` passed on 2026-07-07. That target rebuilt the
Xcode harness, ran the Longhouse unit suite including PreviewSnapshots, and ran
the LonghouseSmoke UI scheme. No backend tests were needed because no backend
code changed.

### Stage 5: Dogfood Handoff Pause

- [x] Summarize exact changes and tests.
- [x] Tell David the exact build/install step needed in Xcode.
- [x] Provide a short dogfood script:
  fresh launch from iOS, send into existing managed session, force transient
  refresh/presence trouble if practical.
- [x] Pause before starting activity-pulse epic.

Dogfood handoff: open `ios/XcodeHarness/LonghouseIOS.xcodeproj` in Xcode,
select the `Longhouse` scheme and David's phone, then build/run. Try one send
into an existing Helm session and one fresh iOS-launched Console session. The
expected behavior is an immediate optimistic user row with `Sending...`, then
`Sent`/`Queued`; if transport confirmation is ambiguous, the row should say
`Could not confirm` and later disappear into the durable transcript once the
same `clientRequestId` appears on the head user event. It should not show
`Send failed` for accepted/delivered messages, and no raw Swift decode text
should appear in the session view.

## Deferred Next Epic

Do not start these until David has dogfooded the receipt correctness epic:

- Derived activity pulse signal.
- Signal Rail animation.
- Packet Crackle preview.
- Liquid Glass polish for liveness surfaces.

## Notes For Future Agents

- Do not treat presence failures as send failures.
- Do not build animation before receipt correctness is dogfooded.
- iOS does not deploy from `git push`; phone dogfood requires Xcode install.
- Worktree is shared. Always commit only explicit files for this epic.
