# No-Python Device Provider Parity

Status: Phase 0 spec draft
Branch: `epic/rust-edge-provider-parity`
Base: `ea8c141a2 Cover opencode managed contract wording`

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
Agent owns native remote launch, send, interrupt, and terminate. It is not yet a
complete no-Python device story if local attach or human launch still requires
the Python CLI. Codex is similar: the bridge/control loop is Rust, but
`longhouse codex` is still Python and therefore remains a device packaging
liability. Claude is the largest remaining native-control port because both the
provider channel bridge and remote control operations still shell out to Python.
Antigravity should remain narrow, but its hook-inbox adapter is also Python and
must be either replaced, embedded as provider-owned hook text without a Python
runtime, or explicitly excluded from the no-Python launch promise until `agy`
offers a better surface.

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

### Decision: Claude is the next real port candidate

**Context:** `engine/src/control_channel.rs` handles Claude launch, send,
interrupt, steer, and answer-pause by spawning `longhouse claude-channel`. The
Python command owns the MCP stdio bridge, HTTP inject endpoint, state file, and
SIGINT interrupt.

**Choice:** Plan a Claude-native Rust channel bridge and launcher stage after
the no-Python device inventory and guardrails.

**Rationale:** Claude is advertised as first-class managed live control, but the
Machine Agent is still a subprocess proxy for the important operations.

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
| Codex | Bridge/control loop is Rust, but `longhouse codex` launch/attach/doctor glue is Python. | Port launcher/attach UX to a compiled Longhouse entrypoint; keep Rust bridge. | The bridge is not the problem; the shipped user entrypoint is. |
| Claude | Python owns `claude-channel` bridge, launch, send, interrupt, steer, answer-pause, MCP config install, and PTY detached launch. | Highest priority provider-control port. | Claude has both live-control and packaging debt. |
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

## Phase Plan

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

### Phase 2: No-Python Device Entrypoint Design

Goal: Replace the Python CLI as the normal provider-control entrypoint without
changing the managed-session UX.

Steps:

1. Decide which compiled binary owns `longhouse <provider>` equivalents:
   `longhouse-engine`, a small native launcher, or `Longhouse.app` helper.
2. Define compatibility shims for existing `longhouse` commands while the
   package transition is in progress.
3. Specify provider binary resolution, env handling, cwd validation, and token
   secrecy once Python is gone.
4. Specify how local health, doctor, repair, provider-live proof, and managed
   launch share the same native support library.
5. Specify what remains in the Python Runtime Host lane and how it is isolated
   from the normal device install.

Success criteria:

- The normal Mac/dev-machine install can start provider sessions without
  invoking Python.
- Any remaining Python command is labeled server-only, test-only, or legacy
  compatibility with a removal phase.
- The design preserves existing CLI UX and machine-control APIs.
- The packaging boundary is reviewed before provider porting starts.

Suggested checks:

- New inventory test for Python entrypoints on the device path.
- Existing provider CLI tests used as behavior fixtures for the native
  replacement.

### Phase 3: Claude Native Channel Bridge and Launcher

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
- Route Machine Agent `claude.launch` through Rust, preserving PTY behavior,
  `--dangerously-load-development-channels`, hook env, and readiness wait.
- Route `claude.send`, `claude.steer`, and `claude.answer_pause` through the
  Rust bridge.
- Route `claude.interrupt` through Rust with process identity validation.
- Keep any Python Claude CLI path as legacy compatibility only, not the normal
  device entrypoint.
- Update provider contracts only if evidence level changes are earned by
  tests/canaries.
- `control_channel.rs` no longer shells out to `longhouse claude-channel` for
  remote launch/send/interrupt/steer/answer-pause.
- Tokens are absent from argv and logs.
- Existing managed Claude UX remains the same for local TUI launch and remote
  control.
- Old state files fail safely or migrate explicitly.
- Focused Claude CLI and engine tests pass.

Suggested checks:

- `cd server && uv run pytest tests_lite/test_claude_channel_bridge.py tests_lite/test_claude_channel_launch_cli.py`
- `make test-engine`
- `make managed-claude-poc` when local provider credentials are available.

### Phase 4: Codex Native Entrypoint and Hardening

Goal: Preserve the good Rust bridge boundary while removing Python from the
normal Codex launch/attach path.

Steps:

1. Port or replace `longhouse codex` launch/attach UX with the native entrypoint
   from Phase 2.
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
- Reapers skip unknown/future state rather than performing destructive cleanup.
- Detached-ui sessions surface failures rather than silently appearing live.
- Codex remains the model for Rust-owned live control plus native human CLI
  entrypoint.

Suggested checks:

- `make test-engine`
- `cd server && uv run pytest tests_lite/test_codex_cli.py tests_lite/test_local_runtime_installer.py`

### Phase 5: OpenCode Native Entrypoint Completion

Goal: Finish the OpenCode no-Python story now that remote control is native.

Steps:

1. Replace local `longhouse opencode` launch/attach/stop compatibility paths
   with the native entrypoint where they are still Python-owned.
2. Preserve the current Rust remote launch/send/interrupt/terminate behavior.
3. Keep active-turn steer and answer-pause unsupported unless provider
   semantics change and proof is added.

Success criteria:

- Normal OpenCode managed launch and attach no longer invoke Python.
- Remote live-control behavior remains unchanged.
- Unsupported operations remain explicit in capabilities and UI/API responses.

Suggested checks:

- `make test-engine`
- Focused OpenCode CLI/bridge tests for launch/attach behavior.

### Phase 6: Antigravity Proof and No-Python Decision

Goal: Decide whether Antigravity ships in the no-Python device path or remains
excluded/narrow until provider mechanics justify a native adapter.

Steps:

1. Replace the hook-inbox adapter with a no-Python implementation, or document
   Antigravity managed send as excluded from the no-Python launch promise.
2. Add plugin install idempotency/concurrency tests.
3. Add hook schema drift tests against captured live/fake `agy` hook payloads.
4. Add capability/error tests proving only send is advertised.
5. Document that launch is local observe/send only; remote launch, reattach,
   interrupt, steer, answer-pause, and terminate remain unsupported.

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

### Phase 7: Product, Packaging, and Evidence Cleanup

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
