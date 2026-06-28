# Native OpenCode Control

Status: Phase 1 approved

## Executive Summary

Longhouse currently treats OpenCode managed sessions as first-class from the Runtime Host, but the Machine Agent still shells out to the Python `longhouse opencode-channel` command for OpenCode send and interrupt. The migration target is engine-native OpenCode control while preserving the existing user-owned provider model: stock `opencode serve` remains the execution owner, Longhouse owns only the bridge state and control path.

The first shippable phase moves only OpenCode send and interrupt into Rust. Remote launch, attach, stop, and lifecycle cleanup stay on the existing Python-backed path until later phases.

## Decision Log

### Decision: Start With Send And Interrupt

**Context:** OpenCode send/interrupt already have stable HTTP semantics through the server bridge: `prompt_async` for input and `abort` for interrupt.
**Choice:** Port those two operations first and leave launch/stop unchanged.
**Rationale:** This removes the hot-path Python shellout while keeping lifecycle ownership and idempotent launch behavior stable.
**Revisit if:** OpenCode changes the local server API or introduces a native mid-turn steering semantic.

### Decision: Keep Launch Python-Backed In Phase 1

**Context:** Launch owns process setup, private runtime config, idempotency locks, token secrecy, and terminal-owned lifecycle modes.
**Choice:** Do not change OpenCode launch in Phase 1.
**Rationale:** Send/interrupt are bounded request adapters; launch has broader lifecycle blast radius and should be ported as its own phase.
**Revisit if:** send/interrupt require state fields that only a native launch writer can safely provide.

### Decision: Keep OpenCode Capability Gating Conservative In Phase 2

**Context:** OpenCode send and interrupt are now native Rust operations, but OpenCode launch still shells out through `longhouse opencode-channel launch`.
**Choice:** Keep the provider-wide `requires_longhouse_cli` gate conservative until native launch lands.
**Rationale:** Splitting per-operation dependency gates would be more precise for send/interrupt, but it adds contract complexity that should disappear once launch moves into the engine.
**Revisit if:** Phase 3 is deferred or users need native send/interrupt on machines with stock `opencode` but no `longhouse` CLI.

## Architecture

Phase 1 adds a Rust OpenCode control adapter in the Machine Agent:

- read `~/.claude/managed-local/opencode-server/{session_id}.json`
- tolerate the existing readable state schema range
- require matching Longhouse session id, provider session id, local server URL, and password
- default the username to `opencode`
- reject non-local or non-HTTP server URLs before sending credentials
- POST send input to `/session/{provider_session_id}/prompt_async?directory={cwd}`
- POST interrupt to `/session/{provider_session_id}/abort?directory={cwd}`
- return the same control result shape the Runtime Host already expects

The Runtime Host and provider contract manifest stay capability-driven. OpenCode active-turn steer remains unsupported.

## Implementation Phases

### Phase 1: Native OpenCode Send/Interrupt

Status: Implemented and approved

Review: Hatch DeepSeek approved commit `7dca166f3` against the Phase 1 acceptance criteria.

Coverage hardening: Added focused regressions for control-channel routing, empty `cwd`, default username, provider-session path encoding, and invalid/incompatible state files.

Goal: Remove the Python CLI shellout for OpenCode `session.send_text` and `session.interrupt`.

Steps:

1. Add focused Rust tests for bridge-state reading and request construction.
2. Add the Rust OpenCode control adapter.
3. Route OpenCode send/interrupt in `control_channel` through the adapter.
4. Remove or update stale `opencode-channel` argument tests for send/interrupt.
5. Run focused engine tests.

Acceptance criteria:

- OpenCode send posts `{"noReply": true, "parts": [{"type": "text", "text": ...}]}` to `/session/{provider_session_id}/prompt_async`.
- OpenCode interrupt posts to `/session/{provider_session_id}/abort`.
- Both requests use Basic auth from the private bridge state file and include the stored `cwd` as the `directory` query parameter when present.
- Non-local OpenCode server URLs are rejected before credentials are sent.
- `control_channel.rs` no longer shells out to `longhouse opencode-channel send` or `longhouse opencode-channel interrupt`.
- OpenCode launch still uses the existing launch path.

Test commands:

```bash
make test-engine
```

### Phase 2: Contract And Gate Cleanup

Status: Implemented

Goal: Make capability advertisement reflect native operation ownership without pretending launch is native.

Verification: `make test-engine` and `make test` passed after adding engine gating/routing coverage and Runtime Host OpenCode dispatch tests.

Acceptance criteria:

- Provider support bits still require stock `opencode` on PATH.
- Provider-wide `requires_longhouse_cli` semantics stay conservative until launch is native.
- Runtime Host tests prove browser/iOS sends route through Machine Agent supports for `opencode.send` and `opencode.interrupt`.
- Engine tests prove OpenCode send/interrupt command frames route natively, and that `opencode` without `longhouse` does not advertise OpenCode support while launch remains CLI-backed.

### Phase 3: Native OpenCode Launch

Status: Planned in branch `epic/native-opencode-launch`

Goal: Port idempotent OpenCode server-bridge launch into Rust.

Scope:

- Replace the Machine Agent's `longhouse opencode-channel launch` shellout for remote `session.launch`.
- Keep `longhouse opencode` attach/TUI, explicit inspect, and explicit stop on the existing Python CLI path until later phases.
- Keep the provider execution owner as stock upstream `opencode serve`.
- Keep the state file schema at version 1 for this stage.
- Do not add an unmanaged import fallback when launch fails.

Implementation plan:

1. Add a Rust launch configuration/result type in `engine/src/opencode_control.rs`.
2. Normalize and validate the Longhouse session id before any process work.
3. Resolve the stock `opencode` binary from `PATH`; explicit debug overrides remain outside this remote launch stage.
4. Acquire a per-session advisory lock under `~/.claude/managed-local/opencode-server/{session_id}.lock`.
5. Under the lock, read existing bridge state and reuse it only when:
   - the state belongs to the same Longhouse session id,
   - the recorded process identity still matches,
   - the local OpenCode health endpoint reports healthy.
6. For a fresh launch:
   - create the runtime plugin/config-content file with the Longhouse runtime events URL, device token, Longhouse session id, and device id,
   - generate the bridge password from the OS CSPRNG,
   - start `opencode serve --hostname 127.0.0.1 --port 0 --print-logs` in the requested cwd,
   - pass credentials and runtime config through environment variables only,
   - tail the server log until it prints the localhost server URL,
   - confirm `/global/health`,
   - call `POST /session?directory={cwd}` with the display title,
   - write private bridge state atomically with dir mode `0700` and file mode `0600`,
   - include every schema-1 field already emitted by the Python writer, including process identity, launch mode, owner wrapper fields, log path, and config content path,
   - return the existing managed-launch payload shape to the Runtime Host.
7. On launch failure after spawning, terminate only the process that was just spawned.
8. Update control capability gating so OpenCode no longer requires the `longhouse` CLI once launch, send, and interrupt are all native. The advertised supports are still computed by the Machine Agent, so old agents keep their old conservative behavior until they are upgraded.

Success criteria:

- `session.launch` for provider `opencode` no longer shells out to `longhouse opencode-channel launch`.
- Launch command argv contains no API token, bridge password, or config content.
- Runtime config content and bridge state are written with private permissions.
- Rust writes to the same `~/.claude/managed-local/opencode-server` state path scanned by `managed_opencode_scan`.
- Existing-live state reuse does not create a second `opencode serve` process.
- Two concurrent launch requests for the same Longhouse session id serialize through the lock and converge on one backing OpenCode server.
- Stale, unhealthy, mismatched, or newer-schema state does not get reused.
- The schema-1 state written by Rust remains readable by the existing scanner and Python CLI attach/stop paths.
- Launch failure returns `provider_launch_failed` and never silently imports an unmanaged session.
- The returned payload remains compatible: `provider=opencode`, `transport=opencode_server_bridge`, `provider_session_id`, `thread_id`, `server_url`, `pid`, and `log_path`.
- Capability advertisement includes `opencode.send`, `opencode.interrupt`, and `opencode.launch` on machines with stock `opencode` even when `longhouse` is absent from `PATH`.

Testing plan:

- Unit-test pure launch helpers: server-log URL parsing, runtime config content composition, state result shape, private atomic JSON write permissions, and provider command resolution.
- Unit-test state parity by deserializing Rust-written bridge state through the scanner-facing state shape.
- Add a fake `opencode` executable integration test that starts a tiny local HTTP server, prints the expected `opencode server listening on ...` log line, and verifies:
  - health and session.create are called with Basic auth,
  - `directory` and title are preserved,
  - secret values are present in env/config where required but absent from argv/state redactions,
  - bridge state is written with schema 1, `launch_mode=detached`, cwd, pid, process identity, log path, and config content path.
- Add a reuse test with a live fake bridge state proving the second launch returns the existing provider session id and does not execute the fake provider again.
- Add a concurrent-launch test proving the lock prevents duplicate backing servers for the same session.
- Add failure tests for missing provider binary, bad cwd/token, server never becoming ready, invalid health, invalid session.create, and newer/mismatched state.
- Add cleanup tests for both spawned-but-unready and already-exited child processes.
- Add control-channel tests proving OpenCode launch routes through the native adapter and provider support no longer requires the `longhouse` CLI.

Acceptance criteria:

- Launch preserves token secrecy, private state permissions, idempotent session locks, and lifecycle modes.
- Existing `attached_tui`, `keep_server`, and `detached` behavior remains compatible with older state files.
- Launch has no hidden fallback to unmanaged import.

Validation:

```bash
make test-engine
make test
```

### Phase 4: Native Stop/Terminate

Goal: Port OpenCode stop/terminate behavior into Rust.

Acceptance criteria:

- Stop uses recorded process identity before signaling.
- PID reuse cannot kill an unrelated process.
- Attached-TUI reaper behavior stays limited to the documented launch mode.

### Phase 5: Full Edge Migration

Goal: Retire Python ownership of managed OpenCode control once send, interrupt, launch, attach, stop, and proof paths are native.

Acceptance criteria:

- Python remains only as CLI packaging/user entrypoint where appropriate.
- Machine Agent owns provider runtime control adapters.
- Docs, SLA config, and release proof reflect the final ownership split.
