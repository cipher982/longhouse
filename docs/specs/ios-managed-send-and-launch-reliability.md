# iOS Managed Send + Launch Reliability

Date: 2026-07-07

## Problem

Two iOS failures looked similar in the app but were different system failures:

1. A follow-up send into an existing managed Codex session in `/Users/davidrose/git/zeta` failed with `The data couldn't be read because it is missing.`
2. A fresh phone-launched managed Codex session in `/Users/davidrose/git/g55` showed the same failure copy after sending the first prompt.

The current UI collapses transport errors, server errors, decode errors, and post-send reconciliation misses into the same raw Swift `localizedDescription`. That makes the user think Longhouse has no "data to send" even when the actual issue is a cloud/control-channel failure or a response/reconcile problem after the prompt already landed.

## Evidence

The zeta follow-up screenshot corresponds to managed Codex sessions that were still locally attached. Hosted session tail around the time contains later terminal-authored user events, but no iOS-authored event with a `client_request_id` matching the screenshot follow-up. Local Codex bridge logs for the same period contain repeated runtime-ingest network failures against `https://david010.longhouse.ai/api/agents/runtime/events/batch` returning Cloudflare 502 HTML. This points to a real hosted/control availability failure for that send path, not just a rendering bug.

The g55 screenshot maps to session `3e619cda-0af4-40cf-b09f-9b00a3622386`. Hosted events show the phone prompt landed as a durable Longhouse-authored user event with `session_input_id=91` and an `ios-*` `client_request_id`, then the assistant ran tools and replied. Local health shows the session as a detached-ui managed Codex bridge. This was a false failure in the iOS surface: the launch/send worked, but the client still showed a failed optimistic bubble.

The iOS send flow already attaches a `client_request_id` to each local optimistic input, and hosted transcript events preserve that identity under `event.input_origin.client_request_id`. However, `SessionViewModel.reconcileSubmittedInputs` only clears inputs in `.submitting`, `.sent`, or `.queued`. If the POST throws after the server accepted the input, the app marks the optimistic bubble `.failed`; later durable events with the same `client_request_id` cannot clear it.

The iOS API client also lets `DecodingError` escape directly. `DecodingError.localizedDescription` is the exact user-facing string from the screenshots: `The data couldn't be read because it is missing.`

Remote launch has a separate compatibility footgun. Current iOS sends an explicit `execution_lifetime`, but the server defaults omitted launch lifetime to `one_shot`. Older clients or scripts that omit it can accidentally ask for one-shot semantics, which require an initial prompt and do not create a continuing live-control session. The execution-lifetime spec says new clients should be explicit while omitted lifetime should preserve the legacy live-control behavior.

## Root Causes

### Existing Managed Session Follow-Up

The zeta send likely failed before the input reached the Runtime Host or before it could dispatch through the managed control path, during hosted 502 availability problems. The product failure was compounded by bad client error handling: the iOS app exposed a Swift decode/transport implementation detail instead of saying the cloud/control path could not confirm delivery.

### Fresh Phone-Launched Managed Session

The g55 launch and first prompt succeeded. The app still rendered a failed optimistic input because the client treats every thrown send as final and does not reconcile `.failed` local inputs against durable transcript events by `client_request_id`.

### Launch Semantics

The launch stack now has two valid managed lifetimes:

- `live_control`: create a continuing Helm-style remote session that the user can keep talking to.
- `one_shot`: run one prompt headlessly and end.

The server default should not choose one-shot for omitted lifetime. That makes old callers silently switch product modes. Clients should choose explicitly; the server should preserve legacy live-control semantics when the field is absent.

## Fix Plan

1. Add iOS domain error copy.
   - Parse both FastAPI `{"detail": {"code"|"error_code", "message"}}` bodies and legacy bare `{"error_code", "error"|"message"}` bodies.
   - Wrap unexpected successful response decode failures as a Longhouse API error with stable copy instead of leaking `DecodingError.localizedDescription`.
   - Make 502/service errors say Longhouse could not confirm delivery, not "Generation failed."

2. Reconcile failed optimistic sends.
   - On send failure, kick a best-effort tail refresh because the server may have accepted the input before the client saw an error.
   - Let `reconcileSubmittedInputs` clear `.failed` inputs when a head-branch durable user event matches the same `client_request_id` or `session_input_id`.
   - Keep the no-identity guard: matching text alone must never clear a failed bubble.

3. Preserve launch compatibility.
   - Change omitted `/api/sessions/launch` `execution_lifetime` back to `live_control`.
   - Keep iOS Launch Session explicit. The segmented control may default to `Run once`, but it must send `one_shot` explicitly.
   - Update endpoint descriptions and tests so one-shot requires an explicit lifetime.

4. Keep the deeper architecture simple.
   - Do not add a second delivery channel or local fallback.
   - Keep `/api/sessions/{id}/input` as the authoritative browser/iOS send endpoint.
   - Keep `client_request_id` as the idempotency/reconciliation key from optimistic UI through durable transcript projection.

## Acceptance Tests

- iOS API tests parse both wrapped and bare structured error bodies.
- iOS API tests map malformed successful `sendInput` responses to stable unexpected-response copy.
- iOS view-model tests prove a failed optimistic send clears after a tail refresh returns a durable user event with the same `client_request_id`.
- iOS view-model tests prove failed bubbles do not clear by matching text without identity.
- Server launch tests prove omitted lifetime creates `session.launch` / `live_control`.
- Server launch tests prove explicit `one_shot` still creates `session.run_once` and still requires an initial prompt.
