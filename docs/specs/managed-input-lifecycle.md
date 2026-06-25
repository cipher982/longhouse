# Managed Input Lifecycle UX

## Problem

Managed session input currently conflates three separate facts:

- **Input delivery**: whether Longhouse saved a user input, dispatched it to the machine/provider, and received a delivery result.
- **Runtime execution**: whether the provider is thinking, running a tool, idle, blocked, or closed.
- **Durable transcript sync**: whether the archived transcript has caught up and contains the authored user row.

This caused two observed failures while dogfooding a managed Codex session launched from iOS and driven from the web:

1. The first web send showed an ambiguous pending/pulsing state for several seconds, then assistant output began streaming.
2. A later send returned a bare `Request failed (422)`.

The first failure is a bad acknowledgement model: the UI should not look stuck while delivery is in progress, but it also must not claim "Sent" immediately on click. The second failure is a validation/error-shaping bug until proven otherwise: FastAPI 422 means the request body failed schema validation, not that the provider rejected delivery.

## Existing Ground Truth

The core lifecycle already exists. This spec does not introduce a new lifecycle table, a new `accepted` status, or a new stream.

`SessionInput.status` is the durable input-delivery source of truth:

```text
queued -> delivering -> delivered | failed
queued -> cancelled
```

`/api/sessions/{session_id}/input` is the JSON text-input route. Its request schema requires non-empty `text`:

```text
SessionInputRequest.text: min_length=1, max_length=10000
SessionInputRequest.client_request_id: optional, min_length=1, max_length=64
```

`/api/sessions/{session_id}/input` already records a `SessionInput` row for managed input, uses `client_request_id` for idempotency, and returns structured outcomes for common control-path failures.

`/api/sessions/{session_id}/inputs-multipart` is the attachment route. It intentionally accepts `text=""` because attachment-only sends are valid composer actions. The route is `auto` intent only in v1.

This means the supported contract is:

- JSON `/input`: non-empty text, zero attachments.
- Multipart `/inputs-multipart`: one or more attachments, optional text.

The composer must never send attachment-only or empty-text submissions to JSON `/input`.

`/api/sessions/{session_id}/inputs` already exposes queued, delivering, and recently failed rows for the composer.

`/api/timeline/sessions/{session_id}/workspace/stream` already emits workspace invalidations over SSE. If lifecycle refresh latency needs improvement, this existing stream should invalidate the inputs query; Longhouse should not add a second `/inputs/stream` surface unless there is measured need.

`/api/sessions/{session_id}/lock` is a concurrency mutex. It is not provider execution truth and should not be the sole source for whether the composer is in a working-turn mode.

`runtime_display` is provider execution truth. It tells the UI whether the provider is executing, idle, blocked, stalled, closed, etc. It is not delivery truth.

Durable transcript rows are archive truth. They are used to de-duplicate optimistic/pending UI against the transcript, not to prove that delivery happened.

Delivered rows are not expected to remain visible through `/api/sessions/{session_id}/inputs`; `list_recent_inputs` intentionally returns queued, delivering, and recently failed rows only. A brief `Sent` confirmation must therefore come from the POST response, not from the inputs read model.

## Product Contract

The composer should make the input path legible as a short state ladder:

```text
click
-> Delivering...
-> Sent | Queued | Failed
-> transcript catches up and de-duplicates the local pill
```

The UI must avoid both lies:

- It must not say `Sent` only because the user clicked.
- It must not keep a vague spinner until the durable transcript row appears.

For managed `auto` and `steer`, the POST may continue to wait for provider/control acknowledgement so inline structured errors such as `turn_ended` remain simple. The UI should label that wait as delivery in progress.

For synchronous `auto` and `steer`, `Delivering...` is the POST-in-flight state. The UI should not expect to observe the transient `delivering` row through `/inputs` for a normal first send, because the POST often creates, dispatches, and marks the row delivered before the next read-model refresh.

For queued input, the UI should show the queued row immediately from the POST response and keep it cancellable until delivery claims it.

For delivered input, the UI should clear the active delivering state and show a brief `Sent` confirmation from the POST response even if no durable transcript row has appeared yet. Transcript reconciliation only removes duplicates once an authored row with matching `session_input_id` or `client_request_id` appears.

For failed input, the UI should show a specific actionable error. Bare framework errors like `Request failed (422)` are not acceptable user-facing managed-control errors.

There are two failure-display cases:

- If the POST fails before a lifecycle row can be rendered, clear the active delivering pill and show a composer-level error banner with the structured message.
- If `/inputs` returns a failed lifecycle row, render it inline as a failed queued-input chip using `last_error`.

Neither case may leave an unlabeled spinner behind.

## Decisions

### Do Not Add `accepted`

Use existing `delivering` for "Longhouse has recorded this input and dispatch is in progress." Adding `accepted` creates an ambiguous boundary that users do not need and increases restart/reconciliation complexity.

### Do Not Split Dispatch Fully Into Background Work

Keep synchronous provider/control acknowledgement for managed `auto` and `steer`. Moving dispatch entirely behind a background worker would convert currently inline structured errors into asynchronous stream failures and widen the crash-recovery window.

Background work remains appropriate for turn-active observation, terminal observation, lock release, and queued-input drain. Those already run asynchronously.

### Do Not Add `/inputs/stream`

Use the existing workspace stream to drive query invalidation, plus the existing `/inputs` endpoint as the lifecycle read model. Add a lifecycle-specific SSE event only if polling/invalidation is demonstrably too slow.

### Treat 422 As A Separate Bug

Before backend contract changes, capture or reproduce the exact 422 body where possible. A plausible trigger is the empty-text asymmetry between JSON `/input` and multipart `/inputs-multipart`:

```http
POST /api/sessions/{session_id}/input
Content-Type: application/json

{"text":"","intent":"auto","client_request_id":"web-..."}
```

FastAPI rejects that request before route code runs because `SessionInputRequest.text` has `min_length=1`. If this is the observed path, the fix is client-side route selection and error shaping: attachment-only submissions must use multipart, and framework validation detail arrays must not collapse into a bare `Request failed (422)`.

However, the current composer already guards the no-text/no-attachment case and routes attachment sends through multipart. If the exact observed body cannot be reproduced from current `handleSend`, the likely live defect is still real but narrower: `ApiError` does not format FastAPI's array-shaped 422 `detail`, so any validation 422 can fall through to `Request failed (422)`.

If evidence shows a different 422 body, update this section before implementing any server contract changes.

## Implementation Plan

### Phase 1: Evidence And Spec

- Capture or reproduce the second-send 422 body, including request path, method, request body shape, and FastAPI validation detail.
- Do not exit this phase until either the exact failing request is pasted into this spec or captured as a failing regression test, or the current code path proves the suspected empty-text JSON request is already guarded and the remaining defect is array-shaped FastAPI validation error rendering.
- Document the current lifecycle and decisions in this spec.
- Have architecture review cover the spec from first principles before implementation.

### Phase 2: Backend Error Shaping

- Add a focused regression for the reproduced 422 path in `server/tests_lite/`.
- If the malformed request comes from client behavior, fix the client and keep a server test that documents the route contract.
- If the server route is too strict for a legitimate composer action, adjust the request model or route handling.
- Keep JSON `/input` text-only and non-empty unless there is a product reason to make empty text meaningful without attachments.
- Keep multipart attachment-only sends valid, with `text=""`.
- Preserve existing idempotency, queue cap, and `turn_ended -> Queue instead` behavior.
- Ensure FastAPI validation detail arrays are converted into useful frontend messages wherever they can surface.

### Phase 3: Composer Lifecycle UI

- Drive the active pending pill from POST in-flight state and POST response for synchronous `auto`/`steer`, and from `SessionInput` lifecycle rows for queued/drain/failed-chip cases. Do not wait for transcript appearance.
- Show a named `Delivering...` state while POST delivery is in flight.
- The `Delivering...` label must be visible DOM text, not only an `aria-label`.
- Show `Queued` for queued rows, with cancel available only while status is `queued`.
- Show a short `Sent` confirmation when a send is delivered, including steer sends that may not create a durable user transcript row.
- Show structured failure details for failed rows and request errors.
- Continue using transcript reconciliation only to de-duplicate the local/pending display against durable `input_origin`.

### Phase 4: Stream Refresh, If Needed

- First rely on existing POST response, `/inputs` query, and workspace stream invalidation.
- If delivered/failed transitions are still visibly delayed, publish a lightweight session input lifecycle invalidation on the existing session workspace stream and invalidate `["session-inputs", session.id]` on receipt.
- Do not add a second EventSource.

### Phase 5: Review, Merge, Push

- Run focused backend tests for session input.
- Run focused web tests for `SessionChat`.
- Run `make test-frontend` for the changed web surface.
- Run `make test` if backend routes/services change.
- Ask for architecture review of the implementation before merging.
- Commit coherent checkpoints during implementation.
- Merge the worktree branch back to `main` and push to `origin`.

## Success Criteria

- **First-send acknowledgement**: A first web send to a freshly launched managed Codex session shows `Delivering...` within one render cycle and does not show an ambiguous unlabeled pulse.
- **Visible DOM acknowledgement**: While the `/input` POST is in flight, the pending pill renders visible text matching `/Delivering/`; an `aria-label` alone is insufficient.
- **Delivery confirmation**: The active POST pending state clears on POST `sent`, POST `queued`, or POST failure without waiting for a durable transcript row. Queued/drain rows continue to reconcile through `/inputs`.
- **Transcript de-duplication**: When a durable user row later appears with matching `session_input_id` or `client_request_id`, the UI does not show duplicate user text.
- **No bare 422**: The reproduced second-send path no longer surfaces `Request failed (422)`. It either does not send a malformed request or renders a useful validation/control message.
- **Attachment-only contract**: Attachment-only sends use multipart with `text=""` and never call JSON `/input`.
- **Turn-ended behavior preserved**: A stale `steer` still produces the existing `Queue instead` affordance.
- **Queue behavior preserved**: Queue cap, cancellation, idempotent `client_request_id`, and queued-input drain continue to work.
- **Restart behavior preserved**: Stale `delivering` rows are repaired by startup reconciliation and do not create permanently stuck composer state. Auto/queue rows remain repairable according to existing drain rules; stale steer rows remain failed on restart rather than silently queued.
- **No new delivery source of truth**: The durable `SessionInput` row remains the input lifecycle authority; `/lock`, `runtime_display`, and transcript rows remain separate signals.

## Required Tests

### Backend

- Reproduce the original 422 body if possible, or document with tests that the suspected empty-text JSON path is already client-guarded and server-invalid by contract.
- Assert JSON `/input` rejects empty text according to the documented text-only contract.
- Assert multipart `/inputs-multipart` accepts attachment-only input with `text=""`.
- `steer` `turn_ended` still returns a structured error that the UI can map to `Queue instead`.
- Idempotent `client_request_id` plus same text returns the existing row/outcome and does not double-dispatch.
- Startup reconciliation repairs old `delivering` rows according to existing rules.

### Web

- `ApiError` formats FastAPI array-shaped 422 `detail` into a useful message, not `Request failed (422)`.
- Slow managed send shows `Delivering...` while the POST is in flight.
- The `Delivering...` assertion checks visible DOM text in the pending pill.
- Delivered response clears the active pending state without a durable transcript row.
- Attachment-only send calls multipart, not JSON `/input`, and renders the delivery lifecycle without a bare validation error.
- Queued response shows `Queued` and allows cancellation while queued.
- Failed response/error shows a structured message instead of an ambiguous spinner.
- Runtime execution can put the composer into working-turn mode even when `/lock` is stale false.

## Open Questions

- Is the current 2-second `/inputs` polling interval enough for queued-drain chip transitions once the primary POST acknowledgement is labeled correctly?
