# Kernel Canonical Sessions

Status: Active implementation
Owner: Longhouse local runtime
Updated: 2026-05-23

## Problem

The menu bar and local-health stack historically mixed several truths:

- Rust engine heartbeats
- Codex bridge state files
- provider process scans
- local managed-session phase ledgers
- legacy `managed_sessions` and `unmanaged_session_bindings` arrays

That let presentation layers re-derive managed versus unmanaged ownership. The
result was visible before launch: the menu bar showed managed Codex app-server
child processes as unmanaged sessions.

The engine now emits `payload.sessions`, a resolved local session view built
from the raw observations it owns. The next cleanup is to make that resolved
view the canonical local session contract for normal health and status surfaces.

## Goal

Make the Rust engine the only normal-path local session identity authority.

`payload.sessions` should drive:

- `longhouse-local-health --fast --json`
- the macOS menu bar app
- normal local health classification
- hosted heartbeat-derived machine/session state, once server ingest consumes it

Python and Swift should be presentation layers. They may format, classify, and
render engine reason codes, but they should not reconcile provider process,
bridge, transcript, and hook evidence into session ownership.

## Success Criteria

- Fast local-health requires `payload.sessions`; legacy arrays are not a normal
  fallback for the menu bar path.
- Deep process scans are diagnostic-only and cannot make the menu bar primary
  health red when the engine-resolved session view is healthy.
- Phase, workspace, bridge, process, and ownership fields used by fast
  local-health are projected by the engine.
- Server heartbeat ingest can consume resolved session rows as the primary
  machine session identity signal while still accepting older engines.
- Legacy `managed_sessions` and `unmanaged_session_bindings` remain accepted
  during transition, but are no longer the preferred read contract.
- Golden fixtures cover `engine-status.json` and
  `longhouse-local-health --fast --json` with only `payload.sessions` present.
- Dogfood shows managed Codex sessions as managed and a deliberately bare
  provider CLI as unmanaged.

## Non-Goals

- Do not design a generic provider abstraction ahead of real provider pressure.
  Keep Codex and Claude behavior explicit.
- Do not delete legacy heartbeat arrays in the first branch. First consume
  `payload.sessions`, dogfood, then quarantine/delete legacy fields separately.
- Do not make broad clippy cleanup part of this work. Reset the clippy baseline
  after the canonical session contract is stable.
- Do not make a schema framework. Small JSON fixtures and contract tests are
  enough for this launch pass.

## Phases

### Phase A: Canonical Fast Status

- Fast local-health reads only `payload.sessions` for session identity.
- Legacy array fallback is removed from the fast/menu-bar path.
- Deep/process-scan logic remains available for explicit diagnostics.
- Add fixtures where `engine-status.json` contains `sessions` but no legacy
  arrays.

Gate:

- `uv run --project server pytest server/tests_lite/test_local_health_cli.py`
- menu bar Swift tests
- live fast health on dogfood machine

### Phase B: Engine Projection Completeness

- Engine resolved rows carry the phase/workspace fields fast local-health needs.
- Python stops re-reading local phase overlay for the fast path.
- Any missing fast-path fields are added to `payload.sessions` rather than
  recovered through Python scans.

Gate:

- `make test-engine`
- `longhouse-local-health --fast --json` fixture/golden parity
- managed Codex dogfood with active, idle, and waiting states

### Phase C: Server Consumption

- Heartbeat ingest treats `payload.sessions` as the primary session identity
  signal.
- Legacy arrays stay accepted and can keep feeding compatibility paths while
  old engines exist.
- Runtime events for unmanaged disappearance are derived from resolved
  unmanaged rows where possible.

Gate:

- heartbeat endpoint tests
- timeline/runtime overlay tests affected by machine liveness
- live dogfood heartbeat against hosted runtime

### Phase D: Quarantine Legacy Arrays

- After soak, stop normal consumers from reading legacy arrays.
- Keep legacy arrays empty or compatibility-only for one release window.
- Then remove the duplicate engine/Python code.

Gate:

- full local-health tests
- `make test-engine`
- hosted smoke on exact SHA

## Review Plan

Run Hatch Opus after Phase A/B and before Phase D. The review question is
whether the engine really owns identity, not whether the code merely passes.
