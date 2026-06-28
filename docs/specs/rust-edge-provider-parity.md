# Rust Edge Provider Parity

Status: Phase 0 spec draft
Branch: `epic/rust-edge-provider-parity`
Base: `ea8c141a2 Cover opencode managed contract wording`

## Executive Summary

The grand vision is not "remove Python from Longhouse." The practical goal is
to remove Python-owned live-control paths from user devices where doing so buys
reliability, simpler repair, safer secrets handling, or more honest capability
advertising.

Python remains acceptable for the Runtime Host, public CLI ergonomics, QA
harnesses, install/update glue, and provider adapters whose stable surface is
itself script/hook-shaped. The Rust Machine Agent should own the device-side
live-control edge whenever the control loop is long-running, security-sensitive,
or required for browser/iOS remote control.

OpenCode is now the reference implementation: the Machine Agent owns native
launch, send, interrupt, and terminate through Rust. Codex is already mostly at
the right boundary: Python starts the human CLI flow, but the long-lived bridge,
app-server relay, send, interrupt, steer, pause-answer, stop, and detached
remote launch are Rust. Claude is the main remaining native-control candidate:
the Machine Agent advertises Claude support, but launch/send/interrupt/steer
still shell out to `longhouse claude-channel`, and the bridge server itself is
Python. Antigravity should stay narrow until the provider exposes a stable
control surface beyond hook-inbox send.

## Definitions

- **Rust edge**: Device-side live-control logic owned by `longhouse-engine`.
- **Python glue**: CLI/API/install/orchestration code that does not own the live
  provider process control loop.
- **Managed provider parity**: Providers share session identity, capability
  language, local-health axes, and proof/evidence rules. It does not mean every
  provider supports the same operations.
- **Live-control path**: The path that launches, sends, interrupts, steers,
  answers, terminates, or keeps a control bridge alive for an active managed
  session.

## Decision Log

### Decision: Keep the goal as Rust-owned live control, not no-Python

**Context:** The product still intentionally has a Python Runtime Host and CLI
surface. Removing all Python would blur the actual launch goal.

**Choice:** Target Python-owned live-control paths on devices, while preserving
Python where it is product glue or test infrastructure.

**Rationale:** This maximizes reliability and install clarity without turning a
focused provider-control epic into a language rewrite.

**Revisit if:** The packaged Python CLI becomes the main source of install or
repair failures after the live-control paths are native.

### Decision: Claude is the next real port candidate

**Context:** `engine/src/control_channel.rs` handles Claude launch, send,
interrupt, steer, and answer-pause by spawning `longhouse claude-channel`. The
Python command owns the MCP stdio bridge, HTTP inject endpoint, state file, and
SIGINT interrupt.

**Choice:** Plan a Claude-native Rust channel bridge stage after Phase 1
guardrails.

**Rationale:** Claude is advertised as first-class managed live control, but the
Machine Agent is still a subprocess proxy for the important operations.

**Revisit if:** Claude removes or materially changes the development channel/MCP
surface before implementation.

### Decision: Do not port Codex just for symmetry

**Context:** The Codex Rust bridge already starts `codex app-server`, fronts it
with a WebSocket relay, persists bridge/session state, and owns remote send,
interrupt, steer, pause-answer, stop, and detached-ui launch.

**Choice:** Keep the current Python/Rust seam. Add hardening tests instead of a
new port.

**Rationale:** Moving the human launcher or version probes to Rust does not buy
meaningful capability or reliability. The durable control loop is already Rust.

**Revisit if:** The Python launcher starts owning long-lived bridge behavior
again, or if install packaging makes the CLI unavailable while the engine is
healthy.

### Decision: Antigravity remains a narrow exception

**Context:** Antigravity control is a hook-inbox adapter around `agy`, and the
provider does not expose stable launch, reattach, interrupt, steer, or terminate
semantics.

**Choice:** Keep Antigravity send narrow and proof-gated. Do not port the
adapter to Rust until a stable provider control surface exists.

**Rationale:** A Rust rewrite of a hook script would add churn without changing
the provider guarantee. The better investment is capability honesty and canary
coverage.

**Revisit if:** `agy` exposes an app-server, socket, channel, or durable session
control API.

## Current Provider Map

| Provider | Current live-control ownership | Recommendation | Why |
|---|---|---|---|
| OpenCode | Rust owns remote launch, send, interrupt, terminate through `opencode_control`; Python remains the local attach/human CLI surface. | Treat as completed baseline. | This is the desired edge shape: Machine Agent support no longer requires the Python CLI for remote live control. |
| Codex | Rust bridge owns app-server, relay, state, IPC send, interrupt, steer, pause-answer, stop, run-once, and detached remote launch. Python owns user-facing launch/attach glue. | Keep seam; harden tests. | The live-control loop is already Rust. Further porting is mostly symmetry work. |
| Claude | Rust control channel shells out to `longhouse claude-channel` for launch, send, interrupt, steer, and answer-pause. Python owns bridge server, state, inject, MCP config install, PTY detached launch, and SIGINT. | Next native port candidate. | Claude is first-class in product copy, but the device-side live-control edge is not yet native. |
| Antigravity | Python hook-inbox adapter; Rust discovers/ships sessions and routes send through the Python channel command. No remote launch, reattach, interrupt, steer, or terminate. | Freeze narrow; improve proof/gating. | Provider mechanics are unstable and hook-shaped; porting now would not create real parity. |

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

## Phase Plan

### Phase 0: Inventory and Spec

Goal: Establish the provider-by-provider truth before building.

Steps:

1. Audit provider contracts, control dispatch, local-health, and provider
   bridge code.
2. Fan out concise provider audits through Hatch DeepSeek.
3. Write this spec with decisions, stages, tests, and success criteria.
4. Have Hatch review the spec.
5. Commit the reviewed spec.

Success criteria:

- The spec names the grand vision and non-goals clearly.
- Each provider has a port/keep/freeze recommendation.
- The next implementation phase is small enough to review independently.
- No runtime behavior changes are included in the Phase 0 commit.

### Phase 1: Contract Guardrails and Test Coverage

Goal: Make the current cross-provider contract harder to accidentally lie about
before moving more code.

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
6. Add doc coverage linking provider parity to the existing managed/unmanaged
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
- No provider behavior changes except clearer errors where tests require them.

Suggested checks:

- `cd server && uv run pytest tests_lite/test_managed_provider_contracts.py`
- `make test-engine`
- Existing script tests that validate provider release proof coverage.

### Phase 2: Claude Native Channel Bridge Design Spike

Goal: Decide the exact Rust implementation shape for Claude before porting.

Steps:

1. Specify Rust state-file schema, permissions, and process identity fields.
2. Specify MCP stdio bridge responsibilities currently in
   `server/zerg/cli/claude_channel.py`.
3. Specify local HTTP or direct IPC inject semantics for send, steer, and
   pause-answer.
4. Specify PID/start-time validation for interrupt.
5. Specify migration compatibility with existing Claude state files.
6. Build a tiny isolated Rust bridge proof if the MCP crate surface is unclear.

Success criteria:

- The design proves a Rust bridge can replace `longhouse claude-channel serve`
  without changing the user-facing `longhouse claude` UX.
- The design includes token secrecy and state permission requirements.
- The design has a rollback path: Python bridge can remain behind an explicit
  debug flag until the Rust bridge is proven.
- Hatch Codex or Claude review approves the design before implementation.

Suggested checks:

- Rust unit tests for state parsing and command construction.
- Python CLI tests that continue to pass while the CLI delegates to the new
  bridge shape.

### Phase 3: Native Claude Launch, Send, Interrupt, Steer

Goal: Move Claude remote live-control operations from Python subprocess proxy to
Rust-owned Machine Agent behavior.

Steps:

1. Implement Rust Claude channel bridge process or module.
2. Route Machine Agent `claude.launch` through Rust, preserving PTY behavior,
   `--dangerously-load-development-channels`, hook env, and readiness wait.
3. Route `claude.send`, `claude.steer`, and `claude.answer_pause` through the
   Rust bridge.
4. Route `claude.interrupt` through Rust with process identity validation.
5. Keep the Python CLI as a user entrypoint or compatibility wrapper, not the
   live-control implementation.
6. Update provider contracts only if evidence level changes are earned by
   tests/canaries.

Success criteria:

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

### Phase 4: Codex Hardening

Goal: Preserve the good Codex boundary while closing test gaps.

Steps:

1. Add or confirm tests for token absence in bridge state, logs, and local
   health output.
2. Confirm future bridge state schema versions and unknown launch modes are
   skipped by reapers.
3. Add detached-ui failure/liveness tests for app-server death or bridge stall.
4. Add subagent-thread rejection fixtures for plausible upstream payload shape
   drift.

Success criteria:

- No Codex code is ported for symmetry.
- Reapers skip unknown/future state rather than performing destructive cleanup.
- Detached-ui sessions surface failures rather than silently appearing live.
- Codex remains the model for Rust-owned live control plus Python human CLI
  glue.

Suggested checks:

- `make test-engine`
- `cd server && uv run pytest tests_lite/test_codex_cli.py tests_lite/test_local_runtime_installer.py`

### Phase 5: Antigravity Proof and Freeze

Goal: Keep Antigravity honest and narrow until provider mechanics justify more.

Steps:

1. Add plugin install idempotency/concurrency tests.
2. Add hook schema drift tests against captured live/fake `agy` hook payloads.
3. Add capability/error tests proving only send is advertised.
4. Document that launch is local observe/send only; remote launch, reattach,
   interrupt, steer, answer-pause, and terminate remain unsupported.

Success criteria:

- `antigravity.send` stays proof-gated.
- Capability output cannot imply reattach or active steering.
- Hook failures produce specific actionable errors.
- The spec records Antigravity as frozen, not neglected.

Suggested checks:

- `cd server && uv run pytest tests_lite/test_antigravity_*.py`
- `python scripts/tests/provider-control-e2e-canary.test.py` if the repo test
  runner supports it directly.

### Phase 6: Product and Evidence Cleanup

Goal: Make product wording, docs, and proof levels match the new ownership.

Steps:

1. Update README/status wording after each provider phase, not before.
2. Update managed-provider skill guidance.
3. Update provider release proof docs only when canary evidence improves.
4. Confirm local-health and doctor output distinguish native control,
   provider support, proof freshness, and release warnings.

Success criteria:

- Public copy remains capability-honest.
- `managed-provider-cli` guidance matches the shipped control paths.
- Evidence levels do not get promoted by implementation alone.

## Testing Strategy

Minimum coverage before implementation:

- Manifest parity tests for provider support bits and operation evidence keys.
- Capability projection tests for true and false provider operations.
- Engine dispatch tests for every support bit.

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

- Rewriting the Runtime Host out of Python.
- Removing the Python CLI as a human/power-user entrypoint.
- Porting provider launchers just for language symmetry.
- Vendoring, pinning, or patching provider CLIs.
- Advertising provider operations before the provider exposes stable semantics
  and Longhouse has proof.

## Hatch Audit Inputs

Phase 0 used four Hatch DeepSeek audits:

- Claude managed-provider path audit.
- Codex managed-provider path audit.
- Antigravity managed-provider path audit.
- Cross-provider contract/proof/local-health audit.

Their outputs are advisory. The decisions above are grounded in the local code
paths and the current provider contract manifest.
