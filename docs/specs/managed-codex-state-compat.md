# Managed Codex State Compatibility

Status: Launch gate
Owner: Machine Agent + managed Codex bridge
Updated: 2026-05-24
Related: `managed-codex-close-lifecycle.md`, `managed-codex-liveness.md`, `remote-session-launch.md`

## Problem

Managed Codex bridge state is a local wire format shared by multiple Longhouse
processes:

- `longhouse-engine codex-bridge run` writes `*.json` state files.
- the Machine Agent scans those files for heartbeat leases and orphan reaping.
- local-health reads them for menu bar and repair status.

These binaries can be briefly out of sync during dogfood, repair, or release.
The unsafe case is a new bridge writer emitting a value an older reaper does not
understand, causing the older reaper to treat a healthy detached-UI session as
an abandoned TUI-attached session and stop it after the grace window.

## Contract

1. Bridge state has an explicit `schema_version`.
   Missing schema versions are legacy and must parse as `0`.
2. New readers must accept old dogfood launch mode values.
   `headless` is interpreted as detached-UI managed Codex, but new writers
   emit `detached_ui`.
3. Readers must be safe by default for future state.
   A live bridge with an unknown launch mode or future schema version must not
   be reaped just because no visible TUI is attached.
4. Missing launch mode is unknown.
   A state file with no `launch_mode` predates the current detached-UI safety
   contract. Treat it as unknown for live-bridge reaping.
5. The product term and persisted writer value are detached-UI managed.
   Docs, user copy, code comments, and new bridge state should use detached-UI
   / `detached_ui` for the lifecycle concept.
6. Binary replacement must be atomic.
   Install/repair writes new engine binaries to a same-directory temporary file
   and atomically replaces the destination.

## Reaper Rule

For live bridge reaping:

- `launch_mode=tui` and no TUI attachment may be tracked and reaped after the
  grace window if idle.
- `launch_mode=detached_ui` or legacy `headless` and no TUI attachment must be
  skipped.
- unknown or missing launch mode must be skipped.
- future schema version must be skipped.

Class-B orphan cleanup still applies when the bridge daemon is dead and the
recorded app-server child is alive; at that point there is no live bridge
control path to preserve.

## Tests

- Reaper unit tests cover `detached_ui`, legacy `headless`, unknown launch mode,
  and future schema version.
- Runtime artifact tests cover atomic local binary replacement.
- Contract tests assert bridge state includes parseable schema and launch-mode
  fields for Python local-health readers.
