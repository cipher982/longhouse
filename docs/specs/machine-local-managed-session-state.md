# Machine-Local Managed Session State

Status: Proposed
Owner: local runtime + desktop
Updated: 2026-04-22

## Goal

Make machine-local managed-session truth a single explicit read model that the
local-health CLI and macOS menu bar can consume without reconstructing state
from multiple artifacts.

The design target is simple:

- provider-owned local state writes current truth
- `longhouse local-health --json` reads that truth
- the menu bar renders that truth

## Non-Goals

This spec does **not** reopen the broader hosted/runtime consolidation that
already landed in `SessionRuntimeState`.

This spec does **not** remove the existing local phase ledger, bridge files, or
engine status payload on day one. Those can continue to exist for shipping,
diagnostics, and cross-checking while the local UI path migrates.

This spec does **not** change the managed phase display contract in
`server/zerg/config/managed_phase_contract.json`.

## Current Shape

Today the local managed-session path looks like this:

```text
Codex bridge state file
Claude channel state file
session_phase_state ledger
process scan
        |
        v
local_health.py reconstructs managed session rows
        |
        v
menu bar renders local-health JSON
```

More concretely:

- Codex bridge writes bridge state in `engine/src/codex_bridge.rs`
- Claude channel writes its own local state in `server/zerg/cli/claude_channel.py`
- hook outbox and Codex bridge both write phase rows into
  `engine/src/state/session_phase.rs`
- `server/zerg/services/local_health.py` combines:
  - process scan for managed liveness
  - Codex bridge state for Codex-specific degradation and orphan detection
  - phase overlay from `session_phase_state`
  - a Codex-only fallback when a fresh phase row has aged out
- the menu bar reads only `longhouse local-health --json`

That is much better than the pre-consolidation state, but it still leaves the
local UI path reconstructing truth instead of reading it.

## Problem

The main defect class is not "wrong label mapping." It is "no single owner for
current local managed-session truth."

That causes four recurring problems:

- `attached + idle` can evaporate when a freshness window expires, even though
  the owner process is still alive
- Codex and Claude reach the same UI through different local-state paths
- `local-health` carries provider-specific reconciliation logic that should
  belong to the provider-owned writer
- local integration tests still need to reason about joins between scans,
  bridge state, and phase rows

The recent "idle session showed THINKING" bug was exactly this class: liveness
stayed true, idle phase aged out, and the UI had to infer what the missing row
meant.

## Decision

Add one canonical machine-local `managed_session_state` projection in the local
SQLite database.

Rules:

- one row per managed session
- provider-owned writers update current truth for their own sessions
- `local-health` reads the projection directly for normal managed-session
  output
- process scan becomes a repair/debug tool, not the primary managed-session
  source for rendering
- phase is a structured enum in the row; display labels remain a separate
  presentation contract

## First-Principles Invariants

- The local UI must read one current fact, not derive one from diagnostic
  artifacts.
- `attached + idle` is a valid steady state and must remain visible while the
  owner heartbeat is fresh.
- Missing phase data must not silently imply active work.
- Provider-specific implementation is allowed. Provider-specific consumer logic
  in `local-health` is not.
- Hosted/runtime truth and machine-local truth are different products:
  - hosted/runtime reducer may use freshness windows to infer liveness from
    sparse events
  - machine-local owner state should remain authoritative while the owner is
    healthy

## Canonical Row Shape

Suggested table: `managed_session_state`

Primary key:

- `session_id TEXT PRIMARY KEY`

Core columns:

- `provider TEXT NOT NULL`
- `workspace_path TEXT`
- `workspace_label TEXT`
- `provider_session_id TEXT`
- `owner_kind TEXT NOT NULL`
- `owner_pid INTEGER`
- `owner_heartbeat_at TEXT NOT NULL`
- `owner_state TEXT NOT NULL`
- `control_state TEXT`
- `phase_kind TEXT`
- `tool_name TEXT`
- `phase_observed_at TEXT`
- `last_activity_at TEXT`
- `last_error_code TEXT`
- `detail_json TEXT NOT NULL DEFAULT '{}'`
- `updated_at TEXT NOT NULL`

Expected enum vocabulary:

- `owner_state`: `attached`, `detached`, `degraded`, `exited`
- `control_state`: `ready`, `unavailable`
- `phase_kind`: `thinking`, `running`, `blocked`, `needs_user`, `idle`, `finished`

Important modeling rule:

- `owner_state` and `phase_kind` are separate axes
- a row may be `attached + idle`
- a row may be `degraded + blocked`
- `phase_kind = NULL` means "owner has not yet reported a phase" or "phase
  unavailable," not "guess working"

Provider-specific details that should not get their own permanent columns yet
can live under `detail_json`.

## Writer Responsibilities

### Codex

Codex bridge becomes the owner of Codex managed-session current truth.

It should update the row when:

- bridge starts
- bridge heartbeat updates
- remote TUI attaches or detaches
- app-server/control readiness changes
- turn starts or completes
- a tool starts or completes
- approval or user-input attention is requested
- a fatal bridge/control error occurs

Codex should write all steady-state fields directly. `local-health` should not
need to inspect a bridge state file to decide whether a healthy attached Codex
session is idle or thinking.

### Claude

Claude channel bridge becomes the owner of Claude managed-session current
truth.

It should write the same row shape Codex writes:

- session identity
- workspace identity
- owner heartbeat
- current phase
- control readiness
- last activity

Claude should stop depending on process scan as the primary source for normal
managed-session visibility once this path exists.

## Consumer Responsibilities

### local-health

`server/zerg/services/local_health.py` should:

- read `managed_session_state`
- transform it into the existing public JSON shape
- continue to use the managed phase contract for display labels

It should no longer reconstruct normal managed-session rows from:

- `_collect_managed_codex_summary(...)`
- `_collect_managed_sessions_by_process(...)`
- `_load_managed_session_phase_overlay(...)`
- `_merge_managed_sessions(...)`

Those code paths should either disappear or move under diagnostics/repair-only
behavior.

### Menu Bar

No architectural change. The menu bar should keep reading
`longhouse local-health --json`.

The benefit is that the menu bar continues to stay dumb: one CLI read, one
render pass, no local provider logic.

### Hosted Runtime

No change.

`SessionRuntimeState` remains the hosted/runtime projection for timeline and
remote clients. It solves a different problem and should not be made to own the
machine-local menu bar path.

## Diagnostics and Repair

This spec does not remove repair visibility.

Diagnostics can still use:

- process scan
- orphan Codex bridge files
- lock-file probes
- engine status
- phase ledger cross-checks

But those become support signals, not the primary steady-state source for the
menu bar.

That split is intentional:

- `managed_session_state` answers "what is true now?"
- diagnostics answer "why might that truth be wrong or missing?"

## Migration Plan

### Stage 1: Add canonical store

- add `managed_session_state` schema and minimal store helpers
- add unit tests for LWW/upsert behavior and state transitions

### Stage 2: Dual-write Codex

- Codex bridge writes canonical rows alongside current bridge state and phase
  ledger updates
- add Codex integration tests for phase and liveness transitions

### Stage 3: Dual-write Claude

- Claude channel bridge writes the same canonical row shape
- add Claude integration tests for the same transition set

### Stage 4: Read canonical state in local-health

- switch normal managed-session output to `managed_session_state`
- keep existing scans and bridge probes only for diagnostics and fallback
  comparison during rollout

### Stage 5: Delete reconstruction path

- remove normal-path joins between process scan, bridge files, and phase ledger
- keep only the diagnostics pieces that still earn their keep

## Test Strategy

This work is only acceptable if it is locked down with end-to-end seam tests.

The goal is to prove not just that each layer works, but that the layers agree.

### 1. Store tests

Add direct tests for `managed_session_state` covering:

- initial write
- newer write replacing older write
- stale write rejected
- null-to-value and value-to-null transitions
- state transitions across `owner_state`, `control_state`, and `phase_kind`

### 2. Provider writer integration tests

Codex integration tests should feed realistic bridge events and assert final
canonical row state for:

- launch -> attached + idle
- turn start -> attached + thinking
- tool execution -> attached + running + tool name
- approval wait -> attached + blocked
- user input wait -> attached + needs_user
- turn complete -> attached + idle
- bridge alive but control unavailable -> degraded
- owner heartbeat stale -> detached or degraded, never thinking

Claude integration tests should cover the same state family through the Claude
channel bridge path.

### 3. local-health integration tests

Given canonical `managed_session_state` rows, assert that
`longhouse local-health --json` emits the correct managed-session payload for:

- happy-path attached idle/thinking/running/blocked/needs_user
- degraded session
- detached session
- missing phase
- stale owner heartbeat
- mixed-provider snapshots

These tests should not require process-scan fixtures for normal healthy cases.

### 4. Menu bar harness tests

Continue harness rendering tests, but source them from canonical local-health
snapshots:

- pill text
- pill color/attention kind
- idle rows with no incorrect thinking pill
- degraded rows with correct warning treatment

### 5. Full seam tests

Add one golden seam test per provider:

- provider event sequence
- canonical row update
- `local-health` JSON read
- menu bar classification/render assertion

This is the regression test that proves the whole path works end to end.

### 6. Edge-case tests

Add explicit regression coverage for:

- attached idle session after long inactivity
- bridge/control daemon alive but app-server unavailable
- owner restart with the same `session_id`
- unknown phase rejected at write time
- orphan transport artifacts surfacing in diagnostics without contaminating
  normal managed-session truth

## Definition of Done

This spec is complete when:

- machine-local managed-session truth is readable from one canonical table
- Codex and Claude both write the same state shape
- `local-health` no longer reconstructs normal managed rows from process scan
  plus phase overlay
- menu bar behavior is unchanged except it becomes harder to drift
- seam tests prove provider events produce the expected rendered local state

## Short Version

Current:

```text
multiple local artifacts -> local_health reconciliation -> menu bar
```

Target:

```text
provider-owned local state -> managed_session_state -> local_health -> menu bar
```

That is the smallest clean design that finishes the local truth consolidation
already underway without reopening unrelated hosted/runtime work before launch.
