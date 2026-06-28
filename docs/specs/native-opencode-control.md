# Native OpenCode Control

Status: Native OpenCode control implemented; evidence cleanup planned

## Executive Summary

Longhouse treats OpenCode managed sessions as first-class from the Runtime Host, and the Machine Agent now owns the OpenCode send, interrupt, launch, and terminate control path natively in Rust. The migration target remains engine-native OpenCode control while preserving the user-owned provider model: stock `opencode serve` remains the execution owner, and Longhouse owns only the bridge state and control path.

The remaining work in this campaign is evidence and documentation cleanup: update public/product wording, SLA/proof notes, and guardrails so they no longer describe OpenCode launch, send, interrupt, or terminate as undefined, Python-owned, or future work. Local attach/TUI helpers remain on the Python CLI path until a separate edge-migration phase decides whether to move them.

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

### Decision: Keep Evidence Conservative After Native Control

**Context:** OpenCode send, interrupt, launch, and terminate are now Machine Agent operations, but proof levels still vary by operation. Some lanes are hermetic or live-no-token rather than full live-token provider proof.
**Choice:** Update ownership and product-scope wording without inflating proof levels.
**Rationale:** Native ownership is a runtime/control-path fact; proof maturity is a separate evidence fact. Mixing them would make release status look stronger than the canaries actually prove.
**Revisit if:** release-proof lanes promote OpenCode remote launch, terminate, or transcript/active-turn behavior to stronger live-token evidence.

## Architecture

The Rust OpenCode control adapter in the Machine Agent:

- read `~/.claude/managed-local/opencode-server/{session_id}.json`
- tolerate the existing readable state schema range
- require matching Longhouse session id, provider session id, local server URL, and password
- default the username to `opencode`
- reject non-local or non-HTTP server URLs before sending credentials
- POST send input to `/session/{provider_session_id}/prompt_async?directory={cwd}`
- POST interrupt to `/session/{provider_session_id}/abort?directory={cwd}`
- start/reuse stock `opencode serve` for remote launch using private runtime config and state files
- stop only identity-matched OpenCode server process groups for managed terminate
- return the same control result shape the Runtime Host already expects

The Runtime Host and provider contract manifest stay capability-driven. OpenCode active-turn steer and pause-answer remain unsupported.

## Implementation Phases

### Phase 1: Native OpenCode Send/Interrupt

Status: Implemented and approved

Verification: Phase 1 acceptance criteria were covered by focused engine tests.

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

Status: Implemented

Verification: `make test-engine` and `make test` passed after adding native launch and gate coverage.

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

Status: Implemented

Verification: `make test-engine`, `make test`, and `make test-ci` passed after adding native terminate routing, PID identity safety coverage, provider contract tests, and ship validation.

Goal: Port OpenCode stop/terminate behavior into Rust.

Scope:

- Add a native OpenCode stop adapter in `engine/src/opencode_control.rs`.
- Add `session.terminate` as the Machine Agent command type for managed
  provider termination.
- Add `terminate` to the machine-control operation mapping and advertise
  `opencode.terminate` only when the upgraded Machine Agent sees stock
  `opencode` on `PATH`.
- Acknowledge that `can_terminate` is already part of the session capability
  model for providers with `terminate=true`; this phase makes the OpenCode
  Machine Agent support bit truthful rather than adding a new UI control.
- Route provider=`opencode` `session.terminate` command frames through the
  native adapter and return the same CLI-compatible shape:
  `exit_code`, `stdout`, `stderr`, `provider`, `transport`, `pid`, `stopped`.
- Preserve the existing Python `longhouse opencode-channel stop` command for
  local CLI attach/TUI cleanup until the final edge-migration phase.
- Do not add a new browser or iOS stop button in this phase. This phase creates
  the truthful lower-level operation; product surfaces can consume it later.

Implementation plan:

1. Extend the provider contract support suffix mapping with `terminate`.
2. Add `opencode.terminate` to the OpenCode manifest entry and update contract
   tests so capability projection and dispatcher routing include terminate.
3. Add `COMMAND_TERMINATE = "session.terminate"` to the Machine Agent control
   channel.
4. Add a Rust `stop_server_bridge(session_id)` adapter that:
   - reads the existing schema-1 OpenCode bridge state,
   - returns `stopped=false` when no recorded PID is present or the PID is no
     longer live,
   - requires recorded process start time and command to be present and to match
     immediately before signaling,
   - refuses to stop legacy state without process identity,
   - never sends a signal when start time or command mismatches prove PID reuse,
   - sends `SIGTERM` only to the process group when the recorded process is
     still its own process-group leader (`getpgid(pid) == pid`),
   - never uses a bare-PID fallback in the native stop path.
5. Route OpenCode `session.terminate` in `control_channel.rs` through
   `stop_server_bridge`.
6. Leave `ManagedOpenCodeReaper` behavior unchanged: it remains an
   `attached_tui` orphan backstop with its own stricter no-bare-PID signal
   rule.

Success criteria:

- Runtime Host capability projection can derive `terminate` from
  `opencode.terminate` in the live Machine Agent `supports[]` handshake.
- `session.terminate` for provider `opencode` no longer shells out to
  `longhouse opencode-channel stop`.
- The native stop result remains compatible with the Python CLI stop payload.
- PID reuse cannot terminate an unrelated process when recorded process start
  time or command differs from the live PID.
- Existing schema-1 bridge state from Python launch and Rust launch is readable.
- Older state without process identity is treated conservatively and returns
  `stopped=false`.
- The terminal-owned reaper still ignores `detached`, `keep_server`, and legacy
  state without owner-wrapper identity.
- No user-facing stop UX is introduced without a separate product decision.

Testing plan:

- Add Rust unit coverage for:
  - identity-matched stop returns `stopped=true`,
  - missing PID / exited PID returns `stopped=false`,
  - mismatched recorded start time returns `stopped=false`,
  - mismatched recorded command returns `stopped=false`,
  - legacy state without identity returns `stopped=false`,
  - identity-matched PID with `getpgid(pid) != pid` returns `stopped=false`,
  - reused PID that is not an OpenCode server is not signaled,
  - control-channel `session.terminate` routes OpenCode through the native
    adapter and rejects unsupported providers cleanly.
- Add contract/dispatcher backend tests for:
  - `machine_control_capability_for_command("opencode", "session.terminate")`
    returns `opencode.terminate`,
  - OpenCode terminate dispatch requires the engine channel support bit,
  - the command frame sent to the Machine Agent has
    `command_type="session.terminate"` and provider=`opencode`,
  - machine directory/local-health operation projection includes terminate when
    the live supports list advertises `opencode.terminate`.
- Re-run existing Python OpenCode CLI stop tests unchanged to prove the
  transitional CLI path still behaves.

Acceptance criteria:

- Stop uses recorded process identity before signaling.
- PID reuse cannot kill an unrelated process.
- Attached-TUI reaper behavior stays limited to the documented launch mode.

Validation:

```bash
make test-engine
make test
```

### Phase 5: Evidence And Docs Cleanup

Status: Planned

Goal: Align public copy, operator docs, SLA config, and proof guardrails with the native OpenCode control path that has shipped.

Scope:

- Update README/status wording so OpenCode is described as managed live control for send, interrupt, launch, and terminate, while still explicitly excluding active-turn steer and pause-answer.
- Update `config/session-propagation-sla.toml` so OpenCode remote send/interrupt is no longer marked undefined, and lifecycle notes no longer say managed OpenCode control is not first-class.
- Update release-proof and provider-roadmap wording where it still frames OpenCode control as pending migration rather than native with conservative proof levels.
- Add lightweight tests or validation checks that catch stale OpenCode product/evidence wording.
- Do not raise operation evidence levels without new proof artifacts.
- Do not add new browser/iOS stop UX in this cleanup phase.

Success criteria:

- Repo docs no longer say OpenCode send, interrupt, launch, or terminate are future, undefined, or Python-shellout-owned.
- Public copy remains honest that OpenCode active-turn steer and pause-answer are unsupported.
- Provider contract manifests still advertise `opencode.send`, `opencode.interrupt`, `opencode.launch`, and `opencode.terminate`, with `requires_longhouse_cli=false`.
- SLA/proof docs distinguish native ownership from proof maturity.
- Tests fail on the stale phrases that caused this cleanup.

Validation:

```bash
make test
make test-ci
```

### Phase 6: Full Edge Migration

Goal: Retire Python ownership of managed OpenCode control once send, interrupt, launch, attach, stop, and proof paths are native.

Acceptance criteria:

- Python remains only as CLI packaging/user entrypoint where appropriate.
- Machine Agent owns provider runtime control adapters.
- Docs, SLA config, and release proof reflect the final ownership split.
