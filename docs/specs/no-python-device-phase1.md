# No-Python Device Phase 1

Status: Draft
Parent spec: `docs/specs/rust-edge-provider-parity.md`

## Goal

Phase 1 makes the no-Python device goal mechanically visible before we port
provider launchers. It should not remove Python yet. It should add guardrails
that answer two questions on every future change:

1. Did provider capability truth drift?
2. Did we add or leave a Python-backed device path without explicitly
   classifying it as transitional/server-only/test-only?

## Scope

In scope:

- Managed provider contract tests.
- Machine Agent support/dispatch parity tests.
- Session capability projection tests for supported and unsupported operations.
- A no-Python device-path inventory script and validation target.
- A baseline of known transitional Python provider-control entrypoints.

Out of scope:

- Porting Claude, Codex, OpenCode, or Antigravity implementation code.
- Removing Python packaging.
- Changing user-facing managed-session behavior.
- Promoting provider evidence levels.

## Design

### 1. Contract Guardrails

The existing contract manifest stays the machine-control ceiling:
`schemas/managed_providers.yml` generates
`server/zerg/config/managed_provider_contracts.json`, and both Python services
and the Rust Machine Agent consume it.

Phase 1 should extend, not replace, the existing tests:

- `server/tests_lite/test_managed_provider_contracts.py`
- `server/tests_lite/test_provider_support_state.py`
- `server/tests_lite/test_session_kernel_capabilities.py`
- `engine/src/control_channel.rs` unit tests

Required checks:

- Every manifest `machine_control_supports` entry maps to a real engine
  dispatch path.
- Every managed live-control dispatch path is represented in the manifest, or
  is explicitly named as internal/non-provider control.
- Existing contract/capability projection tests stay green; Phase 1 should not
  duplicate their coverage unless it exposes a no-Python-specific gap.
- Unsupported operations remain false through the session capability projection.
- `live_control_available`, `host_reattach_available`, `control_path`, and
  `can_resume` remain separate axes.

### 2. Python Device-Path Inventory

Add a repo validation target that produces a stable inventory of Python
entrypoints still used by the normal device path.

The target should distinguish:

- `transitional_device`: currently on the normal user device path; must have a
  replacement phase.
- `server_only`: Runtime Host or hosted/server install lane; not part of this
  provider-control phase.
- `test_only`: QA/test/proof harnesses; allowed.
- `legacy_compat`: old command compatibility retained temporarily; must have a
  removal or replacement phase.

The inventory should fail when:

- A provider has `requires_longhouse_cli: true` in
  `schemas/managed_providers.yml` but has no `transitional_device` inventory
  entry. This manifest field is the current authoritative marker that the
  provider still depends on the Python-packaged `longhouse` CLI for Machine
  Agent remote-control shellouts. Providers with `requires_longhouse_cli:
  false` may still have Python entrypoint, health, proof, or shared-scaffold
  debt; the inventory must classify those separately rather than treating the
  provider as fully no-Python.
- A known `transitional_device` item has no provider, owner area, replacement
  phase, or reason.
- A scanned provider-control Python file is not in the inventory.
- A packaged Python console script under `server/pyproject.toml`
  `[project.scripts]` maps to a device module without an inventory stance.
- A normal device command maps to Python but is marked `server_only` or
  `test_only`.
- A new provider manifest support bit implies a Python-backed device path
  without classification.
- A new provider is added without an explicit inventory stance: no-Python native
  from day one, or transitional with a replacement phase.

The inventory should not fail merely because known transitional Python exists.
That would make Phase 1 unmergeable and would hide the useful signal.

The inventory must also include Rust call sites that transitively invoke the
Python-packaged `longhouse` command. Today those are at least:

- `engine/src/control_channel.rs::run_claude_channel_command`
- `engine/src/control_channel.rs::run_antigravity_channel_command`

Classifying only `.py` files is insufficient because the Machine Agent is the
caller that turns those Python files into active device dependencies.

### 3. Initial Baseline Categories

Expected transitional device items:

- Shared device CLI entrypoint/scaffold: `server/zerg/cli/main.py`,
  `server/zerg/cli/_common.py`, `server/zerg/cli/_launch_ui.py`,
  `server/zerg/cli/_managed_contract.py`
- Claude: `server/zerg/cli/claude.py`
- Claude channel: `server/zerg/cli/claude_channel.py`
- Claude channel helpers: `server/zerg/services/claude_channel_bridge.py`
- Codex launcher/attach wrapper: `server/zerg/cli/codex.py`
- OpenCode launcher/attach wrapper: `server/zerg/cli/opencode.py`
- OpenCode channel compatibility: `server/zerg/cli/opencode_channel.py`
- Antigravity launcher: `server/zerg/cli/antigravity.py`
- Antigravity channel: `server/zerg/cli/antigravity_channel.py`
- Antigravity hook inbox: `server/zerg/services/antigravity_hook_inbox.py`
- Local health/menu-bar/doctor/repair pieces used by the device install:
  `server/zerg/cli/local_health.py`,
  `server/zerg/cli/local_health_fast.py`,
  `server/zerg/services/local_health.py`,
  `server/zerg/services/desktop_app.py`,
  `server/zerg/cli/doctor.py`,
  `server/zerg/cli/machine.py`
- Provider proof entrypoint: `server/zerg/cli/provider_live.py`
- Rust-to-Python transit call sites in `engine/src/control_channel.rs`.

Expected allowed non-device items:

- Provider live canaries and release proof harnesses under `server/zerg/qa/`
  and `scripts/qa/`.
- Backend services that only run inside the Runtime Host lane.
- Tests under `server/tests_lite/` and `scripts/tests/`.
- Embedded fake-provider snippets in Rust test/canary code, such as Python
  shebangs inside `engine/src/codex_app_server_canary.rs`.

## Implementation Steps

1. Add the detailed Phase 1 spec and review it with Hatch DeepSeek.
2. Add or extend contract guardrail tests in the existing Python and Rust test
   files.
3. Add the no-Python device inventory script with a reviewed baseline.
4. Add a Make target, likely `validate-no-python-device-path`, and include it
   in `make validate` once stable.
5. Run focused Make checks.
6. Commit the spec and implementation in small commits.

## Success Criteria

- The repo has a stable command that reports known Python device-path debt.
- The command fails on unclassified provider-control Python files.
- The command fails when a `requires_longhouse_cli: true` provider has no
  transitional-device inventory entries.
- The command reports Rust call sites that shell out to the Python-packaged
  `longhouse` command.
- The command passes with today's known transitional baseline.
- Existing provider contract, support-state, and capability projection tests
  stay green.
- Every managed live-control dispatch path is either manifest-backed or
  explicitly internal/non-provider control.
- No runtime behavior changes ship in Phase 1.

## Suggested Checks

- `make validate-no-python-device-path`
- `make test`
- `make test-engine`
- `make validate-managed-session-contract`
- `make validate` once `validate-no-python-device-path` is wired into it.

If `make test` is too broad for the local machine during iteration, use the
smallest existing Make target that covers the touched layer, then run the broad
target before merge.
