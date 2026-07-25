# Cursor Native Device Parity

Status: shipped incomplete; native release blocked pending remediation

## Problem

PR #62 exposed `cursor` on the installed native `longhouse` facade, while the
Runtime Host already emitted `longhouse cursor --resume-session <id>` for
managed Cursor Helm sessions. The initial port did not preserve the qualified
Python launch, identity, permission, hook, terminal, and test contracts. Python
is deliberately absent from the installed device product, so native Cursor must
remain release-blocked until this document's remediation gates pass.

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

## Native cutover remediation

PR #62 exposed the native command but did not preserve the complete Python
launcher and hook contracts. The native entrypoint remains unavailable for
release until every item below is implemented and proved.

### 1. Fail-closed permission authority

- Once `LONGHOUSE_PERMISSION_HOOK_ENABLED=1`, malformed input, missing launch
  identity, unreadable or malformed claims, mismatched claims, missing
  credentials, transport failures, malformed responses, and expired decisions
  must all emit an explicit Cursor deny response.
- The only inert path is a hook invocation for which remote permission authority
  was never enabled. A successfully read legacy claim with no recorded policy
  remains inert so an old local session cannot silently acquire remote authority.
- Native tests must cover allow, deny, timeout, registration failure, polling
  failure, malformed input, missing claim, mismatched identity, legacy inertness,
  and two identical tool calls receiving distinct request ids. Exact legacy
  stderr diagnostic strings are not part of the contract.

### 2. Transactional identity reservation

- A pending claim may become observed only after managed-session registration is
  known to have succeeded for the same Longhouse session.
- Resuming must retain the recorded permission policy unless the user explicitly
  supplies the same policy. A conflicting explicit policy fails before launch.
  Claims without a recorded policy resume as `provider_local`.
- The facade and engine argument model must represent permission mode as optional
  until resume policy resolution so omitted and explicit values remain distinct.
- Before replacing an observed claim, preserve it as the rollback value. Any
  failure or provider exit before matching hook evidence restores that value;
  a new launch removes its unobserved pending claim.
- Successful promotion preserves registration identifiers and permission policy,
  then removes the rollback file.

### 3. Complete foreground-process ownership

- Every post-`forkpty` error path terminates and reaps the child, removes transient
  socket/state/claim data, and restores the terminal.
- Explicit remote terminate matches the qualified contract: SIGKILL the provider
  and settle the launcher. Forwarding SIGTERM/SIGHUP with bounded cleanup is
  follow-up hardening, not a prerequisite for native parity.
- PTY `POLLHUP` and `POLLERR` settle the relay instead of spinning.
- The public command exits with the provider's real exit status, including exec
  failure and signal termination.

### 4. Preserve stock Cursor terminal behavior

- Capture the real terminal size before `forkpty`, seed the child PTY and
  `LINES`/`COLUMNS`, and continue forwarding SIGWINCH.
- Remove inherited CI-detection variables and repair an absent or `dumb` `TERM`
  exactly as the qualified Python launcher did, because Cursor's Ink renderer
  otherwise changes modes.
- Terminal restoration is proved after normal exit, provider failure, setup
  failure, remote terminate, and launcher signal.

### 5. Restore hook side effects

- Lifecycle hooks continue writing the evidence and phase records consumed by
  Cursor visibility and control.
- Active/idle local presence is emitted through the existing Machine Agent
  outbox contract.
- `afterAgentResponse`, `stop`, and `sessionEnd` wake the exact native Cursor
  `store.db` through `transcript-wake.sock`; ambiguous or absent stores do not.

### 6. Make the release gates honest

- Demote `cursor-managed` from `available` while remediation is in progress.
- Add native launcher and native-hook tests for every behavior above, plus
  malformed socket requests, stale phase, launch-lock contention, send,
  interrupt, terminate, and resize forwarding.
- The installer smoke uses a portable PTY driver on macOS and Linux and proves
  hook installation, output relay, nonzero exit propagation, and no Python/uv
  fallback.
- Restore `cursor-managed` to `available` when the hermetic native parity gates
  and portable installer smoke pass.
- Before release, run the real stock-Cursor product E2E through the installed native facade. It
  must prove launch, resume, idle send, interrupt/recovery, terminate,
  permission allow/deny/fail-closed, archive convergence, and terminal recovery.

### 7. Finish the cutover

- Once native qualification is green, delete the Python Cursor Helm launcher,
  embedded Python hook bodies, their device command wiring, and redundant Python
  hook writer/tests.
- Keep one owner for hook installation and one declaration of the facade-to-engine
  launch arguments. The no-Python device-path validator must reject their return.

### Implementation order

1. Demote the native entrypoint so packaging tells the truth during remediation.
2. Restore fail-closed permission and transactional resume/binding semantics.
3. Restore local presence and exact-store transcript wakes.
4. Correct foreground ownership, provider exit propagation, PTY hang handling,
   and stock TUI environment/geometry.
5. Port the deleted contracts into hermetic native tests and repair the portable
   installer smoke; then restore entrypoint availability.
6. Pass the installed-facade stock-Cursor product E2E.
7. Delete the unreachable Python launcher/hook implementation and strengthen the
   no-Python validator.

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
