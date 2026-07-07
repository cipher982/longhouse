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

- [ ] Read current iOS send path: optimistic bubble, `sendInput`, decode,
  `refreshTail`, and reconciliation.
- [ ] Read current server JSON and multipart input routes.
- [ ] Define exact copy semantics:
  `Sent`, `Queued`, `Could not confirm`, `In transcript`, `Could not send`.
- [ ] Decide whether existing `mobile-tail` plus queued-input routes can prove
  ambiguous sends by `clientRequestId`.
- [ ] If not, write the smallest API delta for receipt lookup.

### Stage 2: Tests First

- [ ] Add iOS test: successful send response followed by refresh failure does
  not mark the submitted input failed.
- [ ] Add iOS test: ambiguous send confirmation moves to checking /
  could-not-confirm instead of terminal failure.
- [ ] Add iOS test: later transcript/input reconciliation by `clientRequestId`
  clears the ambiguous state.
- [ ] Add backend tests only if a receipt lookup/API change is needed.

### Stage 3: Implementation

- [ ] Add or refine `SubmittedInputPhase` / display-state adapter.
- [ ] Preserve strongest-known confirmation for each submitted input.
- [ ] Make post-send refresh failures non-destructive.
- [ ] Make initial fresh-launch load failures non-destructive when launch or
  input succeeded.
- [ ] Remove raw decode/load copy from user-facing send/session messages.
- [ ] Implement narrow backend/API support if Stage 1 proves it is needed.

### Stage 4: Verification

- [ ] Run focused iOS tests while iterating.
- [ ] Run `make test-ios`.
- [ ] Render SwiftUI previews if receipt UI surfaces changed.
- [ ] Run backend focused tests if backend/API changed.
- [ ] Check `git status` and commit only files touched for this epic.

### Stage 5: Dogfood Handoff Pause

- [ ] Summarize exact changes and tests.
- [ ] Tell David the exact build/install step needed in Xcode.
- [ ] Provide a short dogfood script:
  fresh launch from iOS, send into existing managed session, force transient
  refresh/presence trouble if practical.
- [ ] Pause before starting activity-pulse epic.

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
