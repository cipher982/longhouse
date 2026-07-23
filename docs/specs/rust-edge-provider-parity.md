# No-Python Device Provider Parity

Status: Active corrective plan
Branch: `main`
Base: `3dcb66129 Update Helm exit UI expectations`

## Executive Summary

The grand vision is no Python dependency in the shipped on-device Longhouse
control path. The product reason is packaging, not language aesthetics: once the
user's Mac/dev box needs one Python entrypoint, Longhouse inherits virtualenv,
pip/uv, interpreter, native wheel, PATH, and repair burden.

This spec is scoped to provider-control and managed-session device paths. The
Runtime Host may still be implemented in Python while it is operated by hosted,
Docker, or an explicit server install track, but Python must not be a hidden
dependency for the normal device product: Machine Agent, Desktop App, local
health/repair, managed provider launch, attach, send, interrupt, terminate, and
provider proof.

OpenCode is the best live-control reference implementation because the Machine
Agent owns its native managed control path for send, interrupt, and terminate. It is not yet a
complete no-Python device story if local attach or human launch still requires
the Python CLI. Codex is similar: the bridge/control loop is Rust, but
`longhouse codex` is still Python and therefore remains a device packaging
liability. Claude is the largest remaining native-control port because both the
provider channel bridge and remote control operations still shell out to Python.
Antigravity should remain narrow, but its hook-inbox adapter is also Python and
must be either replaced, embedded as provider-owned hook text without a Python
runtime, or explicitly excluded from the no-Python launch promise until `agy`
offers a better surface.

## Why This Is Being Reopened

The product decision was made, but not executed to its finish line. The
resulting failure mode is easy to miss: Rust owns a provider's long-lived
bridge, so individual control-path work appears native, while every human
launch still enters through a Python virtualenv and Python owns the terminal
life cycle and copy. A polished Python exit receipt in July made that mismatch
visible again.

### Timeline

| Date | Change | What it achieved | What it left behind |
|---|---|---|---|
| 2026-06-28 | `8dbb36912` Phase-0 parity spec | Named Rust edge/provider parity. | Treated Python wrappers too generously. |
| 2026-06-28 | `23ab43133` corrected the north star | Explicitly declared no Python on the normal device path. | No executable owner, package, or cutover gate followed. |
| 2026-06-29 | `39c3d41d3` hook inventory | Made installed-hook debt visible. | Did not reject Python from normal launch. |
| 2026-07-01 | `605fda095` shared Python managed-launch core | Reduced wrapper duplication. | Consolidated the transitional layer instead of deleting it. |
| 2026-07-18–22 | Codex lifecycle hardening | Rust bridge ownership and cleanup became more correct. | Python still owned foreground TUI launch and its exit UX. |
| 2026-07-22 | Python exit-receipt polish | Improved the current UI. | Confirmed the public Helm path is still Python. |

This was missed because completion was measured as **native bridge/control
coverage**, rather than the user-visible install → launch → exit path. The
missing release gate was simple: a clean Mac must be able to install, launch,
attach, stop, repair, and inspect a managed provider session without `uv`, a
virtualenv, or `python3`. Until that command proves green, provider work is
transitional regardless of which daemon owns the socket.

## Current Device-Path Python Inventory (2026-07-22)

The Runtime Host remains intentionally Python for this epic. Everything below
is device-product debt because a Mac/dev box must execute it for normal setup,
Helm use, control, or repair.

| Surface | Current owner | Classification | Required destination |
|---|---|---|---|
| `longhouse` executable and command router | `server/zerg/cli/main.py` / Typer | Root dependency: every normal CLI invocation starts Python. | Native `longhouse` device launcher; retain a separate server-admin entrypoint only where needed. |
| Install, onboarding, update, doctor, machine repair, local health | Python CLI plus macOS setup shell script | Device packaging debt; the app setup script installs `uv` and Python. | Rust device/install library surfaced by `Longhouse.app` and native CLI. |
| Codex `launch`, `attach`, `doctor`, foreground TUI, exit receipt | `server/zerg/cli/codex.py` | Transitional; bridge/control loop is already Rust. | Native `longhouse codex` facade in the engine package. |
| Claude Helm launch, config install, and hook migration | `server/zerg/cli/claude.py`, compatibility `claude_channel.py` | The Rust channel server exists, but the normal human path and Python hooks remain. | Native launcher/config migration over the existing Rust bridge. |
| OpenCode Helm launch, attach/stop compatibility | `opencode.py`, `opencode_channel.py`, `opencode_bridge.py` | Python owns normal launch despite native remote controls. | Native Rust launcher/state owner. |
| Cursor Helm PTY launch | `cursor_helm.py` | Native send/interrupt/stop exist, but the foreground launch remains Python; omitted by the earlier provider map. | Native Rust launcher over the existing control adapter. |
| Antigravity wrapper and installed hook | `antigravity.py`, hook text invoking `python3` | Device Python and unstable provider semantics. | Native hook adapter, or explicitly exclude managed send from the device promise. |
| Provider proof and local diagnostics | `provider_live.py`, `local_health.py` | Python is still needed to verify/repair a device. | Native `longhouse provider-live` and `longhouse device …` commands. |

## Target Architecture and Cutover Contract

The device artifact ships two compiled executables from one Rust workspace,
packaged, signed, and versioned together by the app/installer:

1. **`longhouse`** — a Rust human-facing facade. It owns onboarding,
   repair, local health, provider launch/attach/stop, and the terminal UI.
2. **`longhouse-engine`** — the Rust long-lived agent and provider bridge
   implementation. The facade links shared Rust crates rather than spawning
   ad-hoc shell/Python helpers.

The Runtime Host remains a separately packaged Python server command; it must
not be installed as a dependency of the normal device artifact. Compatibility
is explicit and temporary: `longhouse-python` is a separately installed server
and legacy-operator command, never a flag or hidden fallback inside native
`longhouse`.

### Cutover Rules

- The installed `longhouse` on a device is compiled Rust. It never invokes
  Python, `uv`, or a virtualenv for a normal command.
- Provider executables remain user-owned and resolve from `PATH`; existing
  debug overrides stay explicit.
- The native facade owns foreground PTY/TUI process groups and terminal exit
  receipts. A clean exit stops the managed bridge; durable history is retained
  separately.
- Machine-facing Runtime Host calls use a shared Rust client and existing API
  contracts. No browser/client behavior changes are implied.
- Native `longhouse` never discovers, invokes, or falls back to
  `longhouse-python`. A user selects that separately installed command
  explicitly, and it emits a deprecation notice.
- Cutovers use two releases: release N makes the native command default and
  keeps the separately installed legacy command available; N+1 removes that
  provider's Python compatibility path only after upgrade and live-provider
  gates pass. Do not add new user-facing behavior to a Python wrapper during
  the soak.

### Foreground Helm Lifecycle Contract

| Event | Native facade action | Bridge/provider result |
|---|---|---|
| User cleanly exits TUI | Render banked receipt after acknowledged cleanup. | Stop bridge and provider app-server; retain durable thread/archive only. |
| Terminal closes, `SIGHUP`, `SIGTERM`, or `SIGINT` | Forward/handle signal, attempt bounded acknowledged cleanup, then restore terminal state. | No intentional detached provider remains. Failure is explicit and repairable. |
| `SIGTSTP`/`SIGCONT`/`SIGWINCH` | Preserve foreground process-group and terminal resize semantics. | Provider remains attached; no lifecycle claim changes. |
| Provider/bridge/facade crash | Classify from persisted state plus process start identity; show recovery without claiming liveness. | Fail closed for cleanup; never invent a live or stopped state. |
| Upgrade or Machine Agent restart | Pairing/version check and state-schema gate before attach or reap. | Read compatible state, otherwise preserve it and surface repair. |

The Python Codex launcher’s process-group handoff and one-shot recovery behavior
are behavior fixtures for the native implementation, not incidental details.

### Release Gate

Before declaring this complete, CI must prove a fresh macOS device artifact can
perform the following with no `python3`, `uv`, or Python site-packages on PATH:

1. install/repair and start the Machine Agent;
2. launch, cleanly exit, and inspect a Codex Helm session;
3. attach, send, interrupt, and stop a live Codex Helm session;
4. run local health and provider-live proof;
5. perform the equivalent supported operations for Claude, OpenCode, Cursor,
   and the explicit Antigravity decision.

The gate is hermetic: the device-under-test receives a restricted PATH with
trap executables for `python`, `python3`, `uv`, `pip`, and
`longhouse-python`; child-process execution is recorded and any trap invocation
fails the test. The Runtime Host runs outside that device environment. The gate
also exercises DMG install, shell install, upgrade from a uv-installed release,
repair, and uninstall.

## Definitions

- **No-Python device path**: A user can install, repair, launch, observe, and
  remotely control managed sessions on a dev machine without a Longhouse-shipped
  Python runtime or Python package environment.
- **Rust edge**: Device-side live-control and provider-launch logic owned by
  `longhouse-engine`, `Longhouse.app`, or another compiled Longhouse binary.
- **Transitional Python**: Any Python entrypoint used by the device product
  before the no-Python replacement exists. Transitional is allowed only with an
  owner, replacement phase, and test plan.
- **Managed provider parity**: Providers share session identity, capability
  language, local-health axes, and proof/evidence rules. It does not mean every
  provider supports the same operations.
- **Live-control path**: The path that launches, sends, interrupts, steers,
  answers, terminates, or keeps a control bridge alive for an active managed
  session.
- **Server Python**: Python that runs inside the Runtime Host packaging lane.
  Server Python is out of scope for this provider-control spec, but it must not
  be required for the normal on-device Machine Agent/Desktop/provider-control
  path.
- **Installed hook artifact**: Script text Longhouse writes into a provider
  home directory, such as `~/.claude/hooks/longhouse-hook.sh` or
  `~/.claude/hooks/longhouse-permission-gate.py`. Classification follows
  whether the installed script invokes a Python interpreter at runtime, not the
  file extension. A shell hook that invokes `python3` is still device Python
  debt; a shell hook that does not require Python may be `native_exempt`.

## Decision Log

### Decision: Correct the north star to no Python on the device path

**Context:** The original Phase 0 draft treated Python CLI wrappers as
acceptable glue if Rust owned the long-running control loop. That misses the
product wedge: any Python shipped to the user's machine brings the Python
ecosystem management burden.

**Choice:** Treat all Python in the device install/control path as
transitional. The provider-control roadmap must retire or replace Python
entrypoints, not merely move the long-running daemon pieces to Rust.

**Rationale:** A robust Mac/dev-machine product cannot depend on users having a
working Python packaging environment. The native path is an install/support
strategy, not just an implementation preference.

**Revisit if:** Longhouse intentionally chooses a Python-bundled native app or
single-file embedded runtime as the shipping artifact. That would be a packaging
decision, not permission to leave ambient Python dependencies.

### Decision: paired native facade and engine, one Rust workspace

**Context:** `longhouse-engine device …` already owns native health/repair
work, while `longhouse-engine codex-bridge …` owns daemon/bridge internals. A
human-facing CLI must take over the existing `longhouse` name without exposing
daemon internals as its permanent public surface.

**Choice:** Build a public Rust `longhouse` facade and internal
`longhouse-engine` from one workspace and shared library crates. They are
signed, installed, upgraded, and build-identity checked as an atomic pair.
`longhouse-engine` remains the explicit executable for daemon and bridge child
processes; facade-launched bridges resolve that paired engine path rather than
using `current_exe()`.

**Rationale:** One binary would make foreground terminal UX and long-lived
daemon internals one public contract. Two unrelated binaries would duplicate
logic and create version skew. A paired facade/engine split keeps the user
surface small while preserving explicit bridge ownership.

**Revisit if:** A single binary can preserve separate public/internal command
surfaces and safe child-process identity without expanding the user contract.

### Decision: explicit server and legacy Python ownership

**Context:** Today one Python package owns both Runtime Host/server commands and
the device CLI. Reusing the `longhouse` name for native device commands without
splitting this ownership would break operators and make the no-Python claim
ambiguous.

**Choice:** Publish the remaining Python surface as `longhouse-python` for
Runtime Host and explicit legacy-operation use. Native install/repair removes
or quarantines the uv-installed `longhouse` shim after a successful paired
native install; it never silently selects between the two. The command matrix
below is the authoritative migration map.

**Rationale:** A separate executable makes authority and dependency visible.
It prevents a native command from depending on a Python environment that the
device artifact intentionally does not ship.

**Revisit if:** Runtime Host is independently moved to a compiled or bundled
server artifact.

### Command Ownership Matrix

| Command family | Release-N owner | Final owner | Compatibility policy |
|---|---|---|---|
| `longhouse <provider>`, attach/stop, device health/repair, provider proof | Native facade | Native facade | Python equivalent is `longhouse-python` only during release-N soak. |
| `longhouse-engine` daemon and bridge subcommands | Paired engine | Paired engine | Internal/operator surface; facade resolves its exact paired path. |
| Runtime Host `serve`, DB/storage migration, server administration | `longhouse-python` | Server package until a separate server decision | Never part of normal device install. |
| Development generators and test tools | source checkout tooling | source checkout tooling | Not shipped in device artifacts. |

### Decision: Claude native completion follows Codex cutover

**Context:** Rust now owns the Claude channel server and core control commands,
but Python still owns the normal Helm launcher, configuration installation, and
already-installed Python hook migration.

**Choice:** Finish the native Claude launcher/config path after the Codex
facade cutover; retain the existing Rust channel server rather than rewriting
it.

**Rationale:** Claude is advertised as first-class managed live control, but
the human install/launch path still needs Python. Codex is first because its
foreground migration is smaller and proves the facade boundary first.

**Revisit if:** Claude removes or materially changes the development channel/MCP
surface before implementation.

### Decision: Codex bridge is native, but Codex device launch is not done

**Context:** The Codex Rust bridge already starts `codex app-server`, fronts it
with a WebSocket relay, persists bridge/session state, and owns remote send,
interrupt, steer, pause-answer, stop, and detached-ui launch.

**Choice:** Do not port Codex's Rust bridge again, but do port or replace the
Python `longhouse codex` launcher/attach/doctor wrapper as part of the no-Python
device path.

**Rationale:** The durable control loop is already Rust, so the next Codex work
is packaging-boundary work: remove Python as the user-facing way to start and
reattach managed Codex sessions.

**Revisit if:** The Python launcher starts owning long-lived bridge behavior
again, or if install packaging makes the CLI unavailable while the engine is
healthy.

### Decision: Antigravity remains a narrow exception

**Context:** Antigravity control is a hook-inbox adapter around `agy`, and the
provider does not expose stable launch, reattach, interrupt, steer, or terminate
semantics.

**Choice:** Keep Antigravity capability narrow and proof-gated, but do not call
the Python adapter acceptable long term. Either replace the hook-inbox adapter
with a no-Python implementation or exclude Antigravity managed send from the
no-Python launch promise until `agy` exposes a stable provider surface.

**Rationale:** A Rust rewrite does not create new provider guarantees, but a
Python hook adapter still violates the device packaging goal. Capability honesty
and no-Python packaging both need to be true.

**Revisit if:** `agy` exposes an app-server, socket, channel, or durable session
control API.

## Current Provider Map

| Provider | Current device Python exposure | Recommendation | Why |
|---|---|---|---|
| OpenCode | Remote live-control is Rust, but local `longhouse opencode`/attach surfaces still pass through Python CLI code. | Finish no-Python local launch/attach wrappers. | Remote control is good; packaging is not done until the user path avoids Python. |
| Codex | Bridge/control loop is Rust, but `longhouse codex` launch/attach/doctor glue is Python. | First provider cutover: port launcher/attach UX to the native facade; keep Rust bridge. | The bridge is not the problem; the shipped user entrypoint is. |
| Claude | Channel server and core control are Rust; Python still owns Helm launch, config install, and hook migration. | Finish native launcher/config after Codex. | Native bridge alone does not remove device packaging debt. |
| Cursor | Send/interrupt/stop controls are Rust; the foreground Helm PTY launcher is Python. | Port the launcher after OpenCode. | Cursor was omitted from the prior inventory. |
| Antigravity | Python wrapper and hook-inbox adapter own managed launch/send. | Keep capabilities narrow; replace adapter or exclude from no-Python launch promise. | The provider surface is unstable, but Python cannot be treated as a permanent device dependency. |

## Cross-Provider Invariants

1. Capability advertising is data-driven from `schemas/managed_providers.yml`
   and the Machine Agent `supports[]` handshake.
2. Runtime Host may mirror control truth, but it must not invent a capability
   that the connected Machine Agent does not advertise.
3. `live_control_available`, `host_reattach_available`, `control_path`,
   `liveness_model`, and `state` remain separate axes.
4. A provider can be first-class while exposing fewer operations than another
   provider. Unsupported operations must be explicit, not omitted by accident.
5. Tokens must move through env, state files with restrictive permissions, or
   typed local IPC. They must not appear in argv, logs, or bridge payload echoes.
6. Unknown future bridge state schema versions or launch modes must fail closed
   for destructive cleanup.
7. Provider binary ownership stays with the user. Longhouse may wrap/control a
   session, but does not vendor or patch provider CLIs unless there is an
   explicit product decision.
8. The user-facing device product must not require `uv`, `pip`, a virtualenv, or
   an ambient Python interpreter for managed provider control.
9. Generated/installed hook scripts count in the no-Python proof surface. A
   green inventory must not hide Python inside hook templates or installer code.

## Phase Plan

### Corrected Delivery Sequence

The original sequence put Claude before Codex because Claude has the most
Python-owned live control. That is not the fastest way to remove the device
runtime dependency: Codex already has the native bridge and is the smallest
complete vertical slice. Deliver in this order:

1. Establish the compiled `longhouse` facade and a hermetic no-Python device
   test harness.
2. Move Codex launch/attach/exit/stop to that facade, including the terminal
   receipt; make it native-default in release N and remove Python in N+1.
3. Move device diagnostics, repair, and provider-live proof so a Codex device
   does not need Python after exit.
4. Move Claude's launcher/config migration, then OpenCode, then Cursor.
5. Make and enforce the Antigravity include/exclude decision.
6. Delete the Python device package/install path and make the macOS setup
   script install only compiled artifacts.

The delivery sequence above is canonical. The workstreams below define their
dependencies and acceptance criteria; their document order is not execution
order. Every phase is a vertical cutover: artifact, command, behavior fixtures,
packaging, and a release-N compatibility gate. A Rust helper beside a Python
launcher does not count as progress toward the release gate.

### Phase 0: Inventory and Spec

Goal: Establish the provider-by-provider truth before building.

Steps:

1. Audit provider contracts, control dispatch, local-health, and provider
   bridge code.
2. Run concise provider audits against each managed-provider path.
3. Write this spec with decisions, stages, tests, and success criteria.
4. Review the spec before implementation.
5. Commit the reviewed spec.

Success criteria:

- The spec names the grand vision and non-goals clearly.
- Each provider has a port/keep/freeze recommendation.
- The next implementation phase is small enough to review independently.
- No runtime behavior changes are included in the Phase 0 commit.

### Phase 1: Contract Guardrails and Test Coverage

Goal: Make the current cross-provider contract harder to accidentally lie about
and make Python device dependencies visible before moving more code.

Steps:

1. Add a server-side provider capability parity test for
   `schemas/managed_providers.yml`, scoped to the provider's machine-control
   ceiling rather than release/proof readiness.
2. Add Rust control-channel parity tests proving every manifest
   `machine_control_supports` entry has a real dispatch path and every managed
   live-control dispatch path is represented in the manifest or named as
   intentionally internal. This must cover native branches such as
   `session.terminate`, not only the helper that maps support suffixes.
3. Add explicit false-capability tests for OpenCode steer and answer-pause.
4. Add Antigravity minimalism tests proving only `antigravity.send` is
   advertised and unsupported operations fail with provider-specific errors.
5. Add projection tests preserving the separation between
   `live_control_available`, `host_reattach_available`, `control_path`, and
   `can_resume`.
6. Add a no-Python device inventory test or script that lists provider-control
   commands still implemented only by Python entrypoints.
7. Add doc coverage linking provider parity to the existing managed/unmanaged
   product language.

Success criteria:

- Tests fail if a provider advertises a support bit that has no dispatch path.
- Tests fail if a managed live-control dispatch path bypasses the manifest
  without being documented as intentionally internal, such as provider proof or
  archive repair commands.
- Tests fail if unsupported OpenCode or Antigravity operations appear
  steerable/answerable through session capabilities.
- Tests fail if continue/reattach/live-control axes collapse into one boolean.
- The manifest remains the single source of machine-control operation ceilings;
  proof maturity, release readiness, and local live-proof freshness remain
  separate support-state inputs.
- The Phase 1 artifact names every Python command still on the device path and
  assigns it to a later phase.
- No provider behavior changes except clearer errors where tests require them.

Suggested checks:

- `cd server && uv run pytest tests_lite/test_managed_provider_contracts.py`
- `make test-engine`
- Existing script tests that validate provider release proof coverage.

### Phase 2: Native Device Facade and Packaging

Goal: Replace the Python CLI as the normal provider-control entrypoint without
changing the managed-session UX.

Steps:

1. Add the compiled `longhouse` facade from the Target Architecture section,
   sharing Rust crates with `longhouse-engine` rather than duplicating bridge
   logic.
2. Define a deterministic artifact resolver so `Longhouse.app`, shell install,
   and PATH select the same facade/engine build identity.
3. Define compatibility shims for existing `longhouse` commands while the
   package transition is in progress; shims are opt-in and visibly legacy.
4. Specify provider binary resolution, env handling, cwd validation, and token
   secrecy once Python is gone.
5. Specify how local health, doctor, repair, provider-live proof, and managed
   launch share the same native support library.
6. Build the native DMG/shell artifact, nested signing/notarization, launchd and
   systemd paths, atomic upgrade/rollback, uv-shim quarantine, and uninstall.
7. Specify what remains in the Python Runtime Host lane and how it is isolated
   from the normal device install.

Success criteria:

- The normal Mac/dev-machine install can start provider sessions without
  invoking Python.
- `longhouse --version` identifies the compiled device artifact and its paired
  engine build identity.
- A facade refuses a mismatched engine build identity before launching or
  reaping a managed provider session.
- The native installer takes over PATH from an existing uv-installed
  `longhouse` without deleting it until the paired artifact has passed a smoke
  test; uninstall restores or reports the previous explicit state.
- Any remaining Python command is labeled server-only, test-only, or legacy
  compatibility with a removal phase.
- The design preserves existing CLI UX and machine-control APIs.
- The packaging boundary is reviewed before provider porting starts.

Suggested checks:

- New inventory test for Python entrypoints on the device path.
- Existing provider CLI tests used as behavior fixtures for the native
  replacement.

### Claude Native Channel and Launcher Workstream

Goal: Move Claude provider-control operations from Python subprocess proxy to
Rust-owned Machine Agent/native launcher behavior.

Steps:

1. Specify Rust state-file schema, permissions, and process identity fields.
2. Specify MCP stdio bridge responsibilities currently in
   `server/zerg/cli/claude_channel.py`.
3. Specify local HTTP or direct IPC inject semantics for send, steer, and
   pause-answer.
4. Specify PID/start-time validation for interrupt.
5. Specify migration compatibility with existing Claude state files.
6. Implement or spike the Rust bridge if the MCP crate surface is unclear.
7. Replace the Python `longhouse claude` / `claude-channel` device path with the
   native entrypoint from Phase 2.

Success criteria:

- The native bridge replaces `longhouse claude-channel serve`
  without changing the user-facing `longhouse claude` UX.
- The design includes token secrecy and state permission requirements.
- The design has a rollback path: Python bridge can remain behind an explicit
  debug flag until the Rust bridge is proven.
- Keep terminal-originated `longhouse claude` Helm launch behavior unchanged
  while moving its device-side channel machinery to Rust.
- Route `claude.send`, `claude.steer`, and `claude.answer_pause` through the
  Rust bridge.
- Route `claude.interrupt` through Rust with process identity validation.
- Keep any Python Claude CLI path as legacy compatibility only, not the normal
  device entrypoint.
- Update provider contracts only if evidence level changes are earned by
  tests/canaries.
- `control_channel.rs` no longer shells out to `longhouse claude-channel` for
  send/interrupt/steer/answer-pause.
- Tokens are absent from argv and logs.
- Existing managed Claude UX remains the same for local TUI launch and remote
  control.
- Old state files fail safely or migrate explicitly.
- Focused Claude CLI and engine tests pass.

Suggested checks:

- `cd server && uv run pytest tests_lite/test_claude_channel_bridge.py tests_lite/test_claude_channel_launch_cli.py`
- `make test-engine`
- `make managed-claude-poc` when local provider credentials are available.

### Codex Native Entrypoint Workstream

Goal: Preserve the good Rust bridge boundary while removing Python from the
normal Codex launch/attach path.

Steps:

1. Port `longhouse codex`, `longhouse codex attach`, and `longhouse codex stop`
   to the native facade. The foreground TUI launcher owns PTY process-group
   transfer, signal forwarding, bridge cleanup, and terminal receipts.
2. Preserve stock upstream `codex` resolution from PATH plus explicit
   `--codex-bin` / `LONGHOUSE_CODEX_BIN` debug overrides.
3. Add or confirm tests for token absence in bridge state, logs, and local
   health output.
4. Confirm future bridge state schema versions and unknown launch modes are
   skipped by reapers.
5. Add detached-ui failure/liveness tests for app-server death or bridge stall.
6. Add subagent-thread rejection fixtures for plausible upstream payload shape
   drift.

Success criteria:

- Codex launch/attach from the normal device product no longer invokes Python.
- The banked-hearth receipt is emitted by the native foreground launcher, not
  by a Python compatibility wrapper.
- Reapers skip unknown/future state rather than performing destructive cleanup.
- Detached-ui sessions surface failures rather than silently appearing live.
- Codex remains the model for Rust-owned live control plus native human CLI
  entrypoint.

Suggested checks:

- `make test-engine`
- `cd server && uv run pytest tests_lite/test_codex_cli.py tests_lite/test_local_runtime_installer.py`

### OpenCode Native Entrypoint Workstream

Goal: Finish the OpenCode no-Python story now that managed control is native.

Steps:

1. Replace local `longhouse opencode` launch/attach/stop compatibility paths
   with the native entrypoint where they are still Python-owned.
2. Preserve the current Rust Helm send/interrupt/terminate behavior.
3. Keep active-turn steer and answer-pause unsupported unless provider
   semantics change and proof is added.

Success criteria:

- Normal OpenCode managed launch and attach no longer invoke Python.
- Remote live-control behavior remains unchanged.
- Unsupported operations remain explicit in capabilities and UI/API responses.

Suggested checks:

- `make test-engine`
- Focused OpenCode CLI/bridge tests for launch/attach behavior.

### Antigravity Inclusion Decision Workstream

Goal: Decide whether Antigravity ships in the no-Python device path or remains
excluded/narrow until provider mechanics justify a native adapter.

Steps:

1. Replace the hook-inbox adapter with a no-Python implementation, or document
   Antigravity managed send as excluded from the no-Python launch promise.
2. Add plugin install idempotency/concurrency tests.
3. Add hook schema drift tests against captured live/fake `agy` hook payloads.
4. Add capability/error tests proving only send is advertised.
5. Document that launch is local observe/send only; Console execution,
   reattach, interrupt, steer, answer-pause, and terminate remain unsupported.

Success criteria:

- `antigravity.send` stays proof-gated if it remains shipped.
- No shipped Antigravity managed-control path requires Python unless the product
  explicitly excludes it from the no-Python device promise.
- Capability output cannot imply reattach or active steering.
- Hook failures produce specific actionable errors.
- The spec records Antigravity as frozen, not neglected.

Suggested checks:

- `cd server && uv run pytest tests_lite/test_antigravity_*.py`
- `python scripts/tests/provider-control-e2e-canary.test.py` if the repo test
  runner supports it directly.

### Cursor Native Entrypoint Workstream

Goal: Either move the managed Cursor Helm PTY/control-socket path to Rust or
remove it from the normal device product until it can be native.

Steps:

1. Port foreground PTY launch, state ownership, send, interrupt, and stop from
   `cursor_helm.py` to the native facade/engine.
2. Preserve the current provider-owned `cursor-agent` binary and its explicit
   capability limits.
3. Add fake-provider tests covering process-group cleanup and control-socket
   identity checks.

Success criteria:

- Normal managed Cursor Helm launch and recovery do not invoke Python.
- The provider map and installer no longer omit Cursor from no-Python evidence.

### Product, Packaging, and Evidence Cleanup Workstream

Goal: Make product wording, docs, packaging, and proof levels match the new
ownership.

Steps:

1. Update README/status wording after each provider phase, not before.
2. Update managed-provider skill guidance.
3. Update provider release proof docs only when canary evidence improves.
4. Confirm local-health and doctor output distinguish native control,
   provider support, proof freshness, and release warnings.
5. Update install/repair docs so the normal device path does not mention Python,
   `uv`, `pip`, or virtualenv management.

Success criteria:

- Public copy remains capability-honest.
- `managed-provider-cli` guidance matches the shipped control paths.
- Evidence levels do not get promoted by implementation alone.
- A clean machine can install the device product and launch managed provider
  sessions without Python.
- The macOS setup script contains no `uv tool install`, `uv python install`, or
  Python CLI bootstrap for the device product.

## Testing Strategy

Minimum coverage before implementation:

- Manifest parity tests for provider support bits and operation evidence keys.
- Capability projection tests for true and false provider operations.
- Engine dispatch tests for every support bit.
- Device-path inventory tests that fail when a normal managed-provider command
  can only be satisfied by a Python entrypoint.

Minimum coverage during a provider port:

- Unit tests for state parsing, state permissions, token redaction, and process
  identity validation.
- Hermetic fake-provider tests for launch/send/interrupt/steer paths.
- Local-health tests for `control_path`, `liveness_model`, and `state`.
- One provider live canary or documented credential-gated proof before evidence
  level promotion.

Do not chase "complete coverage" by writing broad snapshot tests that encode
implementation details. The useful target is complete behavioral coverage for
the capability contract: if Longhouse advertises it, a test should prove either
the operation works or the unsupported state is explicit.

## Non-Goals

- Rewriting the Runtime Host out of Python inside this provider-control epic.
  Runtime Host packaging is a separate no-Python/server-bundle track.
- Keeping the Python CLI as the normal human/power-user entrypoint.
- Porting provider internals for language symmetry after the device path is
  already Python-free.
- Vendoring, pinning, or patching provider CLIs.
- Advertising provider operations before the provider exposes stable semantics
  and Longhouse has proof.

## Architecture Review Inputs

Phase 0 used four focused provider-path reviews:

- Claude managed-provider path audit.
- Codex managed-provider path audit.
- Antigravity managed-provider path audit.
- Cross-provider contract/proof/local-health audit.

Those audits were advisory and initially over-weighted live-control ownership.
After the product goal was clarified as no Python on the device path, this spec
was corrected to treat Python provider CLI wrappers as transitional debt, even
where the underlying live-control bridge is already native.
