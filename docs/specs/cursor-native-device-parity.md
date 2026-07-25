# Cursor Native Device Parity

Status: proposed

## Problem

The installed native `longhouse` facade does not expose `cursor`, while the
Runtime Host still emits `longhouse cursor --resume-session <id>` for managed
Cursor Helm sessions. The previous Python launcher remains in the repository,
but Python is deliberately absent from the installed device product. This
leaves a supported provider with no working public launch or attach command.

## Product contract

Cursor is a first-tier Longhouse provider. The installed native device must
support these two distinct control planes:

| Plane | Public outcome |
| --- | --- |
| Helm | `longhouse cursor` runs the stock interactive `cursor-agent` TUI in a native-owned PTY. `--resume-session` restores the same Longhouse session and Cursor chat. Remote idle send, interrupt, and terminate work through the existing control socket. |
| Console | The Machine Agent starts and resumes headless Cursor turns through the existing `cursor_print` adapter. It does not require a foreground `longhouse cursor` process. |

Cursor does not support mid-turn steering. The capability contract must state
that explicitly; it must not downgrade Helm or Console support.

## Design

Add a `Cursor` command to the Rust public facade and a native Cursor Helm
launcher. The launcher owns the foreground terminal lifecycle only. It reuses
the existing native components for Cursor identity reservation, binding,
control, transcript ingestion, visibility, and reconciliation.

The port also replaces the two installed Python Cursor hooks. The evidence hook
and the remote-permission hook are part of the native device contract: without
the former, binding and lifecycle evidence is not trustworthy; without the
latter, remote permission approval cannot fail closed. Both hooks must invoke
the paired native engine, following the native Claude hook pattern.

The native launcher must:

1. Require an interactive terminal and preserve stock Cursor TUI behavior.
2. Resolve a user-owned `cursor-agent` binary, accept a cwd and optional
   Longhouse session resume id, and establish the existing managed-session
   registration.
3. Reserve and bind the Cursor chat identity before/after spawn through
   `cursor_launch_binding`.
4. Create the existing per-session socket/state/phase records consumed by the
   Machine Agent and `cursor_helm_control`.
5. Pass bytes in raw terminal mode, propagate resize and termination signals,
   and reliably restore the caller's terminal state.
6. Preserve the existing semantics: idle-only remote send, Ctrl-C interrupt,
   explicit terminate, no active-turn steering, and no provider termination
   merely because Longhouse control or the Runtime Host is unavailable.

Provider liveness and permission safety intentionally have different failure
polarity. Runtime Host/control unavailability must leave the Cursor process
alone; a remote permission decision that cannot be obtained must deny the
individual operation before Cursor runs it.

The native facade will expose:

```text
longhouse cursor [--cwd PATH] [--project NAME] [--name NAME]
                  [--loop-mode assist|autopilot] [--url URL] [--token TOKEN]
                  [--resume-session LONGHOUSE_SESSION_ID]
                  [--cursor-bin PATH] [--config-dir PATH]
                  [--permission-mode remote_approve] [--verbose] [--open]
                  [-- <cursor-agent arguments>]
```

The passthrough rejects Cursor session selectors (`--resume`, `--continue`, and
`--new-session-id`) because Longhouse owns identity. Existing engine control
commands (`longhouse-engine cursor-helm send|interrupt|stop`) remain the single
control implementation for the first slice; façade aliases are not required.
No public Cursor command may fall back to Python or `uv`.

## Delivery plan

1. Document the exact on-disk state/phase fields and socket request/reply wire
   schema shared by `cursor_helm.py` and `cursor_helm_control.rs`; native
   implementation must be byte-compatible with that frozen control contract.
2. Port the PTY launcher in `server/zerg/cli/cursor_helm.py` to a focused Rust
   module. Use a deliberate `libc::forkpty` implementation or an approved PTY
   dependency; the chosen path owns raw-mode restoration on every exit/signal,
   SIGWINCH forwarding, launch locking, pending-binding lifecycle, background
   registration, and terminal-event reconciliation. Keep Python code until
   native behavior is qualified.
3. Port the evidence and fail-closed permission hooks from
   `server/zerg/services/cursor_hooks.py` to native-engine hook subcommands and
   repoint hook installation to those commands.
4. Add `Cursor` parsing and dispatch in `engine/src/longhouse.rs`, preserving
   the full legacy launch surface including `--permission-mode` and trailing
   passthrough arguments.
5. Add `cursor-managed` to `config/native_device_entrypoints.json` and align
   the native-runtime documentation and provider contract claims.
6. Add façade-versus-contract validation: every available native entrypoint
   must parse with the built installed façade, and every generated Runtime Host
   attach command must parse against it.
7. Extend native installer smoke with a fake `cursor-agent` PTY launch and
   command-surface checks. The existing Cursor product E2E already calls the
   native public route and is the release acceptance gate; Gate 0 validates
   Cursor provider mechanics directly and remains a separate prerequisite.
8. After live qualification, remove the Python launcher and make the
   no-Python checker enforce the native Cursor entrypoint.

## Acceptance criteria

- A clean native install recognizes `longhouse cursor --help` and never invokes
  Python, `uv`, or the old Typer command.
- Helm launch and `--resume-session` establish one stable Longhouse/Cursor
  identity and create the expected control state.
- The generated Runtime Host attach command parses against the installed
  façade.
- Existing Console create/resume behavior remains green.
- Hermetic and real-Cursor qualification cover launch, resume, idle send,
  interrupt, terminate, transcript/binding evidence, terminal recovery, and
  permission allow/deny plus fail-closed behavior.
- Native launcher tests cover malformed socket requests, stale state, launch
  lock contention, resize forwarding, and terminal restoration after failure.
- Mid-turn steer is explicitly represented as unsupported.

## Non-goals

- Adding mid-turn steering to Cursor.
- Replacing the stock Cursor interactive TUI with ACP or another UI.
- Shipping Python as a hidden compatibility dependency.
