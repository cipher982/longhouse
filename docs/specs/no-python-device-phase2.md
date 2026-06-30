# No-Python Device Phase 2

Status: Draft
Parent spec: `docs/specs/rust-edge-provider-parity.md`
Previous phase: `docs/specs/no-python-device-phase1.md`

## Goal

Phase 2 defines the native device entrypoint that will replace the Python
`longhouse` CLI as the normal way to install, repair, launch, attach, inspect,
and prove managed provider sessions on a user's machine.

This phase should not port every provider yet. It should create the product and
technical contract that later provider phases implement:

1. Which compiled binary owns the normal device command surface?
2. Which Python commands remain as temporary compatibility shims?
3. How do launch, health, repair, provider proof, and provider binary
   resolution share one native support boundary?
4. What Python remains in the Runtime Host/server lane and how is it kept out
   of the normal device install promise?

## Product Decision

The native device command owner is `longhouse-engine`.

`longhouse-engine` is already the shipped compiled Machine Agent binary on the
user's dev machine, already owns shipping, control-channel support, Codex
bridge commands, and native OpenCode remote control. Phase 2 extends that
product contract with a planned `device` command namespace for human/script
entrypoints:

```text
longhouse-engine device <command> [args...]
```

Phase 2A adds the native namespace scaffold and read-only plan/status commands.
Later phases implement it command group by command group. The macOS `Longhouse.app` remains the
recommended human surface and may invoke native device commands internally. The
Python `longhouse` console script remains only as a compatibility shim until
package transport is replaced.

## Phase 2A Namespace Scaffold

Phase 2A establishes the compiled owner without claiming provider behavior has
been ported:

```text
longhouse-engine device plan [--json]
longhouse-engine device status [--json]
```

The Rust binary embeds `config/native_device_entrypoints.json` at compile time,
so installed `longhouse-engine` can report the device-entrypoint contract
without needing a repo checkout or Python interpreter. The owner may be marked
`native` once this namespace exists. Individual command groups must remain
`planned` until their corresponding Phase 1 Python inventory entries stop being
`transitional_device` or `legacy_compat`.

Repo authoring and CI validation for this contract may remain Python tooling in
this stage. The no-Python promise here applies to the installed runtime command
path on the user's device.

## Non-Goals

- Rewriting the Runtime Host out of Python.
- Removing PyPI/`uv tool install longhouse` in this phase.
- Porting Claude's channel bridge, Antigravity's hook-inbox adapter, or all
  provider wrappers in this phase.
- Changing the current user-visible managed-session UX.
- Vendoring, pinning, or patching provider CLIs.

## Native Command Shape

The target native command namespace is:

```text
longhouse-engine device <command> [args...]
```

Normal compatibility command:

```text
longhouse <command> [args...]
```

During transition, the Python CLI may delegate to `longhouse-engine device ...`
for commands that have been ported. It must not be the long-term source of
truth for device behavior.

Initial target groups:

| Group | Legacy UX | Native target | Later provider phase |
|---|---|---|---|
| Device root | `longhouse`, `longhouse --help`, shared CLI scaffolding | `longhouse-engine device` | Phase 2 |
| Desktop App | `Longhouse.app` setup/status/menu-bar flows invoking Python CLI helpers | Consumer of `longhouse-engine device ...`, not an owner | Phase 7 implementation |
| Local health | `longhouse local-health`, `longhouse-local-health` | `longhouse-engine device local-health` | Phase 7 implementation |
| Doctor/repair | `longhouse doctor`, `longhouse machine repair`, `longhouse connect --install` | `longhouse-engine device repair` as the consolidated native repair surface | Phase 7 implementation |
| Provider proof | `longhouse provider-live ...` | `longhouse-engine device provider-live ...` | Phase 7 implementation |
| Claude | `longhouse claude`, `longhouse claude-channel ...` | `longhouse-engine device claude ...` plus native channel bridge | Phase 3 |
| Codex | `longhouse codex` | `longhouse-engine device codex ...` over existing Rust bridge | Phase 4 |
| OpenCode | `longhouse opencode`, compatibility channel/bridge helpers | `longhouse-engine device opencode ...` over existing Rust OpenCode control | Phase 5 |
| Antigravity | `longhouse agy`, `longhouse antigravity-channel ...` | `longhouse-engine device antigravity ...` or explicit exclusion | Phase 6 |

## Behavioral Invariants

1. **Same UX, native owner.** Existing `longhouse <provider>` workflows keep
   their flags and visible behavior unless a later phase explicitly changes
   them.
2. **User-owned provider binaries.** Native commands resolve `claude`, `codex`,
   `opencode`, and `agy` from the user's PATH by default. Provider-specific
   env/debug overrides remain explicit and documented. No `longhouse-codex`,
   bundled provider runtime, or provider release-asset lane is introduced.
3. **No token argv.** Runtime tokens and provider bridge tokens move through
   environment variables, state files with restrictive permissions, or typed
   local IPC. They must not appear in argv, process titles, logs, or generated
   shell snippets.
4. **CWD validation stays strict.** Native launch commands reject missing,
   relative, or disallowed working directories before spawning provider CLIs.
5. **Python compatibility is visible.** Any Python compatibility shim must
   remain in the no-Python device inventory as `legacy_compat` or
   `transitional_device` with a replacement/removal phase.
6. **Server Python is isolated.** Runtime Host Python remains in the hosted or
   self-host server lane. It cannot be used to satisfy normal Machine Agent,
   Desktop App, local-health, repair, or managed-provider launch behavior once
   that native command is marked `native`.
7. **Capabilities stay provider-specific.** Native entrypoint parity does not
   make unsupported provider operations supported.

## Phase 2 Implementation Artifact

Add a native device-entrypoint contract file:

```text
config/native_device_entrypoints.json
```

The contract records:

- the native owner binary and namespace;
- the temporary Python compatibility scripts;
- each legacy device command and its native target command;
- the relevant no-Python inventory IDs from Phase 1; this is a many-to-one
  relationship because one command plan can replace several Python files and
  Rust-to-Python call sites;
- the later implementation phase responsible for actually porting behavior;
- provider binary ownership and token/cwd policies.

Each command plan has these fields:

- `id`
- `status`: `planned`, `native`, `transitional_shim`, or `excluded`
- `implementation_phase`
- `legacy_commands`: one or more current user/script commands
- `native_target_command`: the planned compiled command
- `phase1_inventory_ids`: one or more Phase 1 inventory items covered by the
  plan
- `providers`: `all` or a provider list
- `provider_binary_ownership`: `user_owned`, `not_applicable`, or
  `excluded_until_provider_surface`
- `token_policy`: `env_or_state_file`, `no_token`, or `not_applicable`
- `cwd_policy`: `strict_absolute_or_existing`, `inherits_existing`, or
  `not_applicable`
- `notes`

Phase 2A marks only `native_owner.status` as `native`. Command entries start as
`planned`. A later phase may mark an entry `native` only after the
corresponding Phase 1 Python inventory item is no longer
`transitional_device`/`legacy_compat`.

## Phase 2B Native Fast Local Health

Phase 2B adds the first real device behavior under the Rust namespace:

```text
longhouse-engine device local-health [--json] [--state-root <path>]
```

This command is intentionally a fast, read-only status projection. It reads the
Machine Agent status file at `~/.longhouse/agent/engine-status.json` or
`<state-root>/agent/engine-status.json`, computes freshness from file metadata,
and reports a small native health snapshot without invoking Python.

The JSON contract includes:

- `schema_version`
- `collection_tier: native_fast`
- `health_state`, `headline`, and reason codes
- `engine_status` path/existence/freshness/error details
- synthesized `spool` pending/dead counts from the engine payload
- managed-session count from the engine payload
- `control_channel` and `build` only when already present in
  `engine-status.json`

Phase 2B does not replace the rich Python `longhouse local-health` collector,
the `longhouse-local-health` menu bar helper, doctor, repair, provider proof,
or provider launch behavior. The `local-health` command plan therefore remains
`planned`; its notes record that native fast status exists while full parity
remains future work.

Add a validation target:

```text
make validate-native-device-entrypoints
```

The validator should fail when:

- a Phase 2 `native-device-entrypoint` inventory item has no native
  entrypoint plan;
- a packaged Python console script has no compatibility-shim plan;
- a plan references an unknown no-Python inventory ID;
- a `native_target_command` is still `python`, `uv`, `pip`, or the Python
  `longhouse` CLI;
- a provider command plan does not state user-owned provider binary policy;
- a token-bearing command plan does not state a non-argv token policy;
- a native command is marked complete while the Phase 1 inventory still marks
  the corresponding Python path as transitional.

This creates a bridge from Phase 1's debt ledger to the actual native command
surface that later phases will implement.

## Phase 2C Native Repair Planning

Phase 2C adds the first native repair decision surface without performing
repair:

```text
longhouse-engine device repair-plan [--json] [--state-root <path>]
```

The command is read-only. It reads:

- `~/.longhouse/agent/engine-status.json`, or
  `<state-root>/agent/engine-status.json`;
- `~/.longhouse/machine/state.json`, or
  `<state-root>/machine/state.json`.

It then reports a native recommendation:

- `healthy` when the fast engine status is healthy and machine state is
  configured;
- `machine_repair` when canonical machine state is complete but the local
  status file is missing, stale, or unreadable;
- `connect_install` when canonical machine state is missing, unreadable, or
  incomplete;
- `inspect_logs` when the machine is configured and the issue is a
  transport/status inspection problem rather than a repairable configuration
  gap.

The JSON contract includes `schema_version`, `collection_tier`,
`read_only`, `recommendation`, `headline`, `reasons`, `machine_state`,
`engine_health`, `suggested_actions`, and `notes`. The `machine_state` object
reports only path, boolean completeness fields, and non-secret read/parse
errors; it must not echo the runtime URL, machine name, device token, or any
other secret-bearing value.

Phase 2C does not replace `longhouse doctor`, `longhouse machine repair`, or
`longhouse connect --install`. The `doctor-repair` command plan remains
`planned` until a later phase ports the write-capable install and repair flow.

## Phase 2D Native Existing-Service Repair

Phase 2D adds the first write-capable native repair action:

```text
longhouse-engine device repair [--json] [--dry-run] [--state-root <path>]
```

This command is intentionally narrow. It can restart an existing configured
Machine Agent service when native state proves that the command is touching the
same Longhouse install:

- macOS: `launchctl kickstart -k gui/$UID/com.longhouse.shipper`
- Linux: `systemctl --user restart longhouse-shipper`

The command reads the same fast engine status and canonical machine state as
`repair-plan`, then inspects the existing launchd plist or systemd user unit.
When `--state-root` is provided, the service file must declare a matching
`LONGHOUSE_HOME`; otherwise native repair refuses to run. Machine state output
continues to report only boolean completeness and path/error metadata. Runtime
URL, machine name, device token, and token-like values are not echoed.

The JSON contract includes:

- `schema_version`
- `collection_tier: native_fast_write`
- `dry_run`
- `state`: `completed`, `dry_run_planned`, `failed`,
  `rejected_connect_install`, `rejected_no_service`,
  `rejected_service_mismatch`, or `rejected_unsupported_platform`
- `headline`
- `actions` with restart command/status/error metadata
- `machine_state`
- `service` path/existence/platform and `LONGHOUSE_HOME` match metadata
- `before_health`
- `after_health` only after an attempted successful restart
- `notes`

Phase 2D does not create or rewrite service files, install hooks, regenerate
Desktop App artifacts, replay backlog, rotate tokens, write machine state, or
kill provider/engine processes directly. Those remain later repair/install
parity work under the `doctor-repair` command group.

## Phase 2E Native Service Artifact Repair

Phase 2E should add the next deliberately scoped write-capable native repair
action: creating or repairing the Machine Agent service manager artifact from
already-configured canonical machine state.

Proposed command shape:

```text
longhouse-engine device repair --repair-service [--json] [--dry-run] [--state-root <path>]
```

This is intentionally explicit. A plain `device repair` keeps the Phase 2D
behavior: restart only an existing service whose `LONGHOUSE_HOME` positively
matches the target Longhouse home. `--repair-service` opts into writing the
service file when the machine is already configured.

Native service repair may:

- read canonical machine state from `~/.longhouse/machine/state.json`;
- reject missing, unreadable, or incomplete machine state;
- generate the canonical macOS launchd plist or Linux systemd user unit for
  `longhouse-engine connect`;
- include non-secret service environment such as `CLAUDE_CONFIG_DIR`,
  `LONGHOUSE_HOME`, `LONGHOUSE_LOG_DIR`, `PATH`,
  `LONGHOUSE_MACHINE_GENERATION`, and `LONGHOUSE_MACHINE_STATE_HASH`;
- start/load the service through the platform service manager after writing
  the artifact;
- report dry-run actions without writing or spawning.

Native service repair must not:

- create or mutate `machine/state.json`;
- create, read, write, echo, or rotate the device token;
- install Claude/Codex/Antigravity hooks;
- install or regenerate `Longhouse.app`;
- replay backlog;
- migrate legacy shipper DB state;
- remove legacy service names;
- kill provider or engine processes directly;
- install global services for scratch `LONGHOUSE_HOME` overrides.

The first implementation should target the stable default Longhouse home only:
`~/.longhouse`. If `--state-root` or `LONGHOUSE_HOME` points somewhere else,
native service repair should return a structured rejection instead of creating
a global service for scratch state. Tests may still use injected home/service
paths to exercise generation logic without touching the real user service.

The generated service should match the current Python service contract closely
enough that either implementation can inspect and restart it:

- macOS path:
  `~/Library/LaunchAgents/com.longhouse.shipper.plist`
- Linux path:
  `~/.config/systemd/user/longhouse-shipper.service`
- service command:
  `longhouse-engine connect --fallback-scan-secs 300 --spool-replay-secs 30
  --archive-repair-mode <paused|drain> --compression zstd --machine-name <name>`
- hosted `*.longhouse.ai` Runtime Hosts default archive repair mode to
  `paused`; other Runtime Hosts default to `drain`.

The JSON result should extend Phase 2D with action ids such as
`write_service_file`, `load_launchd_service`, `systemd_daemon_reload`,
`systemd_enable_service`, and `systemd_start_service`. It should report paths,
booleans, action status, and redacted/truncated service-manager errors, but
must not echo runtime URL, machine name, or token values.

The JSON result should also include `repair_mode: service_artifact` so callers
can distinguish service artifact repair from Phase 2D restart-only repair.
Expected states are:

- `dry_run_planned`
- `completed`
- `failed`
- `rejected_scratch_home`
- `rejected_machine_state_unreadable`
- `rejected_machine_state_incomplete`
- `rejected_existing_service_mismatch`
- `rejected_existing_service_ambiguous`
- `rejected_engine_executable_unavailable`
- `rejected_unsupported_platform`

Stable-home resolution is part of the safety boundary. The public command may
write only when the effective Longhouse home resolves to canonical
`~/.longhouse`. It must reject when `--state-root`, `LONGHOUSE_HOME`, or
`CLAUDE_CONFIG_DIR` would target a scratch Longhouse home. Internal tests may
inject alternate home/service roots, but the user-facing command must not
install a global launchd/systemd service for scratch state.

Phase 2E needs internal access to service-generation fields, but not broad
machine-state behavior. It should read only whitelisted fields from
`machine/state.json`, ignore unknown fields (including accidental token-shaped
fields), sanitize the machine name with the same semantics as Python, and
compute `LONGHOUSE_MACHINE_STATE_HASH` from the same whitelisted facts as
Python: `schema_version`, `runtime_url`, `machine_name`,
`desktop_app_enabled`, and `desired_bundle_version`. It may include existing
`config_generation` in service env when present, but it must not rewrite
machine state just to create a generation.

Allowed filesystem writes are limited to:

- the service file parent directory;
- the service artifact file itself;
- the stable-home agent log directory used by `LONGHOUSE_LOG_DIR` and
  launchd `StandardOutPath` / `StandardErrorPath`.

The service command must use an absolute `longhouse-engine` executable path.
Real stable-home writes should prefer the installed runtime binary and reject
missing or dev/build-tree executables unless an internal test hook explicitly
injects an executable path. The result may report executable path/source
metadata, but no secret-bearing machine facts.

Before rewriting an existing service file, native repair must reject symlinks
and non-regular files, require the canonical service path, require a positive
`LONGHOUSE_HOME` match, and verify the file looks like the Longhouse Machine
Agent service rather than an arbitrary user file.

Service-manager sequencing should mirror the current Python contract without
legacy cleanup:

- macOS: unload/boot out only `com.longhouse.shipper` when present, write the
  plist atomically, then load/bootstrap the plist.
- Linux: write the unit atomically, run `systemctl --user daemon-reload`, enable
  `longhouse-shipper`, and start/restart it so changed command/env takes
  effect.

Service-manager stdout/stderr may echo generated commands. Native repair should
redact the runtime URL and machine name from errors before reporting them, then
truncate as Phase 2D already does.

## Success Criteria

- The repo has a reviewed Phase 2 spec that chooses the native owner and shim
  strategy.
- `longhouse-engine device plan` and `longhouse-engine device status` report
  the embedded native device-entrypoint contract without invoking Python.
- `longhouse-engine device local-health [--json] [--state-root <path>]` reports
  a read-only native fast health snapshot from `engine-status.json` without
  invoking Python.
- `longhouse-engine device repair-plan [--json] [--state-root <path>]` reports
  read-only native repair recommendations from engine status and machine state
  without invoking Python, reading tokens, writing files, or spawning repair
  subprocesses.
- `longhouse-engine device repair [--json] [--dry-run] [--state-root <path>]`
  can restart only an existing configured launchd/systemd Machine Agent service
  when service `LONGHOUSE_HOME` matches the target state root.
- Native repair rejects incomplete machine state, missing service files,
  mismatched/ambiguous service homes, and unsupported service managers without
  attempting fallback process killing or artifact regeneration.
- `longhouse-engine device repair --repair-service [--json] [--dry-run]`
  creates or repairs only the stable-home launchd/systemd Machine Agent service
  artifact from existing canonical machine state, then starts/loads the service
  through the platform service manager.
- Native service repair rejects scratch Longhouse homes, missing/incomplete
  machine state, unsupported service managers, and mismatched existing service
  homes without touching hooks, desktop app artifacts, tokens, backlog, or
  machine state.
- `config/native_device_entrypoints.json` names the native target for every
  normal device command category from the Phase 1 inventory.
- `make validate-native-device-entrypoints` passes and is included in
  `make validate`.
- Tests fail if a new packaged Python console script or Phase 2 inventory item
  lacks a native plan.
- Tests fail if a native target command points back through Python.
- Phase 2B local-health tests cover fresh, missing, stale, unreadable, and
  alternate state-root status files.
- Phase 2C repair-plan tests cover configured/no-op, configured repair,
  unconfigured connect-install, transport-only inspection, corrupt state, and
  alternate state-root inputs.
- Phase 2D repair tests cover dry-run, successful restart, restart failure,
  macOS launchd service matching, Linux systemd service matching, missing
  service, unconfigured machine state, service home mismatch, service home
  ambiguity, unsupported platform, and no secret echo.
- Phase 2E service-repair tests cover dry-run, stable-home rejection/acceptance,
  macOS plist generation, Linux unit generation, service-manager action
  sequencing, existing matching service rewrite, mismatched service rejection,
  hosted archive repair mode defaulting, self-host archive repair mode
  defaulting, no token-file requirement, machine-state hash parity with Python,
  symlink/non-regular service rejection, no machine-state/journal mutation,
  service-manager failure reporting, and no secret echo.
- Provider launch, repair, provider proof, and rich local-health/menu-bar
  behavior remain planned until their implementation phases.

## Suggested Checks

- `make validate-native-device-entrypoints`
- `make validate-no-python-device-path`
- `make validate-makefile`

Provider implementation phases will run provider-specific engine/server tests.
Phase 2 itself should stay a contract/design layer.
