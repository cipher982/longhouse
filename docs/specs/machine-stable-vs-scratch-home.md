# Machine Stable Vs Scratch Home

Status: Active
Owner: local machine surface
Updated: 2026-04-23

## Goal

Keep one trustworthy daily-driver Longhouse machine while still allowing
aggressive local experimentation.

The design target is intentionally narrow:

- one protected stable machine contract
- one disposable scratch Longhouse home
- one control-plane truth for managed launch
- zero accidental cross-contamination between them

## Product Rule

Longhouse should not pretend there are rich first-class machine profiles yet.

For now there are only two lanes:

1. **Stable home**
   - canonical path: `~/.longhouse`
   - owns the real machine identity
   - may install global integrations:
     - machine agent service
     - Claude/Codex hooks
     - menu bar app

2. **Scratch home**
   - any non-canonical Longhouse home, typically `LONGHOUSE_HOME=~/.longhouse-dev`
   - disposable local state for debugging and dogfood experiments
   - must not mutate global integrations by default

## Invariants

1. Stable managed launch must use one coherent control-plane target.
2. Scratch work must never silently rewrite the stable service, hooks, or app.
3. A scratch home may install runtimes and persist its own machine/token state.
4. A scratch home is not a second full machine install.
5. Health and launch checks must agree on whether the current home is stable or scratch.

## Phases

### Phase 1: Unified Launch Readiness

Already landed.

Scope:

- one shared managed-launch readiness reducer
- CLI preflight for managed Claude/Codex
- doctor and local health use the same split-brain truth
- stable home refuses localhost control-plane retargets when the runner is enrolled elsewhere

Success criteria:

- split-brain launch fails before the API call
- doctor reports the same root cause
- scratch homes do not inherit false runner mismatch failures

### Phase 2: Scratch Home Isolation

Scope:

- classify the active Longhouse home as stable or scratch
- treat scratch homes as disposable local state roots
- skip global integrations in scratch mode:
  - service install/reconcile
  - Claude/Codex hook install
  - menu bar install
- surface the skip honestly in CLI output

Success criteria:

- `LONGHOUSE_HOME=~/.longhouse-dev ... --install` does not touch the stable service
- scratch reconcile does not rewrite stable hooks or menu bar
- CLI output says the work stayed local/scratch

### Phase 3: State Diet And Alerting Cleanup

Scope:

- remove or demote ghost machine-state fields from critical decisions
- tighten alerting around managed-launch readiness vs soft local drift
- keep `runtime_url` as a control-plane fact, not a catch-all topology bucket

Success criteria:

- `runner_enabled` and `topology_intent` are no longer load-bearing for launch safety
- red states mean managed launch is actually broken
- yellow states mean degraded but still usable

## Non-Goals

- no first-class profile system
- no second background daemon
- no automatic shadow install of a second launchd/systemd service
- no attempt to make the scratch lane look production-ready
