# Managed Codex Close Lifecycle

Status: Ready for implementation
Owner: CLI wrapper + Machine Agent + Runtime Host
Updated: 2026-05-09
Related: `managed-codex-liveness.md`, `session-lifecycle-liveness-contract.md`, `session-runtime-display-contract.md`

## Problem

Managed Codex sessions can briefly show `Blocked Control Path` after a mobile
SSH terminal disappears. The common case is an iPhone Termius session where the
user taps close or the app is backgrounded/suspended. From the server side this
usually looks like the SSH/PTTY control channel vanished. That is not enough
evidence to distinguish deliberate user close from network loss, app
suspension, or terminal crash.

The current stack has a good final-state backstop: the Machine Agent reaps an
idle detached Codex bridge and posts `terminal_state=session_ended`. The
problem is the path and copy before that terminal signal:

- the Python `longhouse codex` wrapper only stops the bridge after the attached
  Codex TUI returns to normal control flow;
- if the wrapper itself is killed by terminal hangup, it may never call
  `codex-bridge stop`;
- the Rust reaper then waits for its grace window before stopping an idle
  bridge;
- hosted runtime state shows repeated `blocked/control path` phase signals
  during the gap, which reads like the session still needs user action.

## First Principles

1. PTY hangup does not carry durable human intent.
   Treat SIGHUP/SSH drop/Termius close as `terminal_disconnected` unless the
   user closed the session through a Longhouse command or API.
2. Explicit Longhouse close is the only source of `user_closed`.
   Browser, iOS, or CLI close actions may send this reason because they are
   product-level intent.
3. Detached-UI launch is not terminal detachment.
   Browser/iOS remote launch intentionally has no visible Codex TUI. The
   absence of a TUI is only close evidence for TUI-attached managed sessions,
   not for detached-UI managed sessions that already created a bridge thread.
4. Detached with an active turn is not closed.
   If Codex is still in-progress, the bridge remains reattachable.
5. Detached and idle TUI-attached sessions should close quickly and honestly.
   Once there is no TUI attachment and no active turn, Longhouse should avoid
   prolonged `blocked/control path` UI. It can terminalize as
   `terminal_disconnected` after a short grace.
6. Terminal reason is part of the contract.
   `session_ended` answers lifecycle. `terminal_reason` answers why.

## Terminal Reasons

Use these public runtime reasons:

| Reason | Producer | Meaning |
| --- | --- | --- |
| `user_closed` | Explicit Longhouse close command/API | User intentionally ended the managed session through Longhouse. |
| `terminal_disconnected` | CLI wrapper signal handler or idle-detached reaper | The attached terminal/control client disappeared without explicit product intent. |
| `bridge_stop` | Generic/manual bridge stop fallback | Compatibility value when no more specific reason is known. New stop paths should not emit it. |
| `provider_signal` | Provider terminal event | Provider reported terminal state directly. |
| `process_gone` | Machine process snapshot | Process disappearance was confirmed. |
| `host_expired` | Runtime policy | Host was offline too long; this is not process death. |

`terminal_disconnected` closes lifecycle, but UI copy should not imply blame or
failure. It should read as a clean detached-terminal end state.

## Desired Flow

### Normal local TUI exit

1. `longhouse codex` starts bridge.
2. Wrapper attaches the stock Codex TUI with `--remote`.
3. TUI exits and returns control to the wrapper.
4. If no active turn survived, wrapper calls:
   `longhouse-engine codex-bridge stop --session-id ... --reason terminal_disconnected`.
5. Bridge posts `terminal_state=session_ended`,
   `terminal_reason=terminal_disconnected`.

### Explicit Longhouse close

1. User chooses close in a Longhouse surface or CLI command.
2. Caller sends bridge stop with `--reason user_closed`.
3. Bridge posts `terminal_state=session_ended`, `terminal_reason=user_closed`.

This spec only implements the lower-level reason plumbing. Product close UI can
be wired separately.

### SSH/PTTY hangup while wrapper is alive

1. Wrapper receives SIGHUP/SIGTERM.
2. Signal handler attempts one best-effort bridge stop with
   `terminal_disconnected`.
3. Handler restores signal default and exits with the signal code.

The handler must be short and non-recursive. If cleanup fails, the reaper still
owns the backstop.

### SSH/PTTY hangup that kills the wrapper

1. TUI attachment disappears.
2. Machine Agent observes bridge state.
3. If the bridge was launched as a TUI-attached managed session, no TUI is
   attached, and no turn is active, reaper stops bridge with
   `terminal_disconnected` after the grace window.
4. If an active turn is still in progress, reaper leaves it alone for
   reattach.

Detached-UI managed sessions are excluded from this close path. They have no
visible TUI by design and remain steerable through the bridge unless the bridge
daemon itself dies or an explicit Longhouse close arrives.

## Implementation Plan

1. Extend the Rust bridge stop contract.
   - Add `terminal_reason: Option<String>` to `BridgeStopConfig`.
   - Add `--reason` to `longhouse-engine codex-bridge stop`.
   - Add `reason` to the IPC stop JSON:
     `{"kind":"stop","reason":"terminal_disconnected"}`.
   - Decode missing or unknown IPC reasons as `bridge_stop` so old CLIs,
     old daemons, and staged upgrades remain available.
   - Carry the reason through IPC `Stop`.
   - Use the reason in `post_terminal(...)` and transcript wake source detail.

2. Extend the Python Codex wrapper.
   - Change `_stop_native_codex_bridge(session_id=...)` to accept `reason`.
   - Normal TUI exit uses `terminal_disconnected`.
   - Add a temporary signal cleanup scope around `_run_native_codex_tui`:
     install handlers, hand the TTY foreground process group to the child,
     wait, restore TTY ownership, then restore handlers.
   - On SIGHUP/SIGTERM, attempt bridge stop with `terminal_disconnected`
     once. The handler is best-effort; the reaper remains the durable
     backstop.
   - Bound signal-path stop cleanup with a short timeout. Do not let a dying
     terminal or stuck IPC socket hang wrapper termination.
   - Do not emit Warp OSC events from the signal handler.
   - Preserve the existing active-turn-survived logic.

3. Extend the reaper.
   - Class A idle-detached reap calls stop with `terminal_disconnected`.
   - Class B orphan app-server cleanup remains process cleanup; if it can post
     runtime terminal later, use `process_gone`, not `user_closed`.

4. Carry the reason through reducer/display/client projection.
   - Runtime already closes on explicit terminal state.
   - Ensure `terminal_disconnected` remains a closed lifecycle reason in
     `session_liveness_facts`.
   - Preserve `terminal_disconnected` in `session_runtime_display` instead of
     collapsing it to `provider_signal`.
   - Extend TypeScript client unions in `web/src/services/api/agents.ts`.
   - Avoid special UI work unless tests reveal a copy regression.

5. Tests.
   - Rust: bridge terminal event carries custom stop reason.
   - Rust: CLI `codex-bridge stop --reason terminal_disconnected` maps into
     config, and missing/unknown reasons degrade to `bridge_stop`.
   - Rust: old-format IPC stop without `reason` still posts `bridge_stop`.
   - Rust: reaper live-bridge stop uses `terminal_disconnected`.
   - Python: normal TUI exit calls stop with `terminal_disconnected`.
   - Python: nonzero exit with active turn keeps bridge alive.
   - Python: signal cleanup handler attempts stop once with
     `terminal_disconnected`.
   - Python: signal cleanup plus normal exit does not double-stop.
   - Backend liveness: `terminal_disconnected` closes lifecycle and preserves
     the reason.
   - Runtime display/liveness facts: `terminal_disconnected` remains explicit.

## Compatibility

The bridge daemon can outlive the CLI that launched it. During rollout:

- new CLI to old daemon: the old daemon ignores the new IPC `reason` field and
  emits `bridge_stop`;
- old CLI to new daemon: missing `reason` defaults to `bridge_stop`;
- signal handler and reaper may race; duplicate terminal events are acceptable
  as long as reducer protection keeps `session_ended` final.

`bridge_stop` is therefore expected in telemetry for older sessions, but new
code paths should prefer a specific reason.

## Non-Goals

- Do not infer Termius X button intent from PTY/SIGHUP alone.
- Do not patch or fork Codex.
- Do not remove the reaper grace entirely; it still protects active-turn and
  startup races.
- Do not introduce a second lifecycle source outside runtime terminal events.

## Open Questions

- Should Longhouse expose a direct "Close session" UI on cards/workspace that
  sends `user_closed`?
- Should idle-detached reaper grace shrink from 120s after dogfood confirms
  active-turn reattach remains reliable?
