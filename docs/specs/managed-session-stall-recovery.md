# Managed Session Stall Recovery

Status: Draft for launch implementation
Owner: David Rose
Last updated: 2026-04-28

## Problem

Managed sessions can remain visually `thinking` or `running` for hours after the provider has stopped making progress. The harmful case is not a long-running shell command; it is an active managed session with no active tool, no transcript/runtime progress, and a still-addressable control path.

The product must make this state visible and recoverable. It must not interrupt legitimate long-running tools such as test suites.

## Definitions

- **Long tool**: `phase=running` with `active_tool` populated. Example: `Bash` running an end-to-end suite for 45 minutes. This is not a stall.
- **Provider stall**: `phase in {thinking,running}`, `active_tool` empty, no runtime progress past the stale threshold. This is the failure mode observed in managed Claude sessions.
- **Soft interrupt**: transport-specific interrupt (`claude-channel interrupt`, Codex bridge interrupt). It is equivalent to asking the provider to stop the current turn.
- **Terminate**: killing the provider process/control path. This is destructive and manual or opt-in only.

## Launch Behavior

1. Runtime display marks a managed active no-tool session as `stalled` only after Longhouse's existing runtime freshness window has expired. This is intentionally conservative for launch.
2. Timeline/session detail render `Stalled` with recovery copy.
3. Browser session detail exposes `Interrupt` for stalled or active managed sessions.
4. The existing machine API interrupt remains available for automation.
5. Auto-interrupt is a later background policy. The launch slice only adds state, visibility, manual recovery, and an interrupt result that can be counted.

## Stall Predicate

A session is stalled when all are true:

- `control_path=managed`
- runtime presence state is `thinking` or `running`
- `active_tool` / `presence_tool` is empty
- runtime view confidence is `stale` or the runtime source is stale/fallback
- no explicit terminal state exists

A session is not stalled when an active tool is present, even if it has been running for a long time.

Launch threshold source:

- For `thinking`, stale means the existing `PHASE_FRESHNESS["thinking"]` window expired.
- For `running`, stale means the existing `PHASE_FRESHNESS["running"]` window expired.
- No additional short timeout is introduced in this slice.

This means a slow first token is not marked stalled until the normal phase freshness window expires, and a long test suite is not marked stalled because it carries an active tool.

## UI Contract

Runtime display adds:

```json
{
  "state": "stalled",
  "tone": "stalled",
  "headline": "Stalled",
  "detail": "No progress from the provider",
  "is_executing": false,
  "needs_attention": true,
  "is_stalled": true
}
```

`stalled` is a recoverable active-control state, not a lifecycle closure.

## Recovery Controls

Session detail shows an `Interrupt` button when live control exists and the session is active/stalled. Clicking it calls the browser endpoint:

```http
POST /api/sessions/{session_id}/interrupt-live
```

The endpoint dispatches the same transport-aware interrupt as the machine route and releases any session lock. Success means the interrupt command ran; it does not guarantee the provider stopped.

Implementation requirement: the browser endpoint must delegate to `interrupt_managed_local_session()` and must not duplicate provider-specific transport logic.

Repeated clicks are allowed. The endpoint remains idempotent from the UI's perspective: a second interrupt either dispatches another soft interrupt or returns the transport failure.

The response carries:

- `interrupt_dispatched`
- `confirmed_stopped=false`
- `exit_code`
- `released_lock`
- `error`

## Non-Goals For This Slice

- No automatic hard kill.
- No SMS/iOS push yet.
- No transcript-file mtime shipping in heartbeat yet.
- No background auto-interrupt daemon yet.
- No short-window stall classification for healthy slow model first-token latency.

## Follow-Up

- Add transcript size/mtime to machine heartbeat for stronger stall detection.
- Add optional auto-soft-interrupt after 60 minutes stalled no-tool.
- Add desktop/iOS notification when a session transitions to stalled.
- Add observability row for currently stalled managed sessions.
- Add per-transport progress sources: Claude transcript mtime/size, Codex rollout JSONL/bridge status, and later Gemini equivalent.
