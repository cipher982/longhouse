# Local Agent Health Plane

Status: Active
Owner: local machine surface
Updated: 2026-04-13

## Goal

Keep Longhouse's hot path brutally simple:

- hooks write local state and return immediately
- the Rust engine owns batching, retry, and remote shipping
- users get an explicit local health surface when shipping is degraded

This document defines the local health plane that sits beside the transport plane. It also defines the modular seam that lets Longhouse keep the CLI and machine surface stable while the macOS product grows from a launchd-managed ambient helper into a proper app-owned local product.

Packaging and release mechanics for that surface live in `docs/specs/distribution-update-loop.md`.
The macOS product decision lives in `docs/specs/macos-launch-product-shape.md`.
The product-facing app/install unification plan lives in `docs/specs/local-app-product-unification.md`.

## Vision

Longhouse on a user machine should feel like a real local product, not an invisible background daemon.

The user should always be able to answer three questions quickly:

1. Is Longhouse healthy on this machine right now?
2. If not, is the problem local daemon health, remote connectivity, or durable dead-letter/backpressure?
3. What is the next action to recover?

The user should not have to infer health from missing sessions, stale presence, or a log file grep.

## Product Principles

- **Do not block the coding session on remote shipping.** A broken shipper must not stall Claude/Codex tool execution.
- **Do not hide degraded shipping.** Local-only hot paths require an explicit health plane.
- **Keep transport and health separate.** Hooks write local. Engine ships remote. Health surfaces observe both.
- **Preserve a stable contract.** UI surfaces must depend on a stable local-health schema, not launchd or plist internals.
- **Prefer ambient surfaces over transcript pollution.** Menu bar, status view, and one-shot warnings are better than fake in-band chat messages.
- **Fail new managed launches before lying.** Managed launch can fail fast if local health is broken. Ongoing sessions should degrade visibly, not be interrupted.

## Current State

Today the raw pieces already exist:

- hooks write presence JSON to `~/.claude/outbox`
- the engine drains outbox every second
- the engine writes `~/.claude/engine-status.json`
- the engine service is supervised by launchd on macOS with `KeepAlive=true`
- `longhouse doctor` and `longhouse connect --status` already expose fragments of the picture

What is missing is a user-facing local health plane.

### Fresh lesson from April 8

The first real machine failure after the menu bar MVP work was not a dead daemon. It was a **coherence failure**:

- local CLI URL pointed at `127.0.0.1:8080`
- runner daemon was healthy against `david010.longhouse.ai`
- CLI machine label was `cinder.local`
- runner identity was `cinder`

Shipping looked mostly healthy in isolation, but managed launch was broken. That means the health plane cannot stop at daemon/outbox status. It must also surface **identity and launch-readiness coherence**.

### What is actually risky

The main failure is **silent degradation**, not infinite outbox growth.

- Presence outbox files are ephemeral and pruned when stale.
- The daemon already does startup recovery and fallback scans when it returns.
- launchd already restarts simple crashes.

The real product problem is that the user may keep working while Longhouse is degraded and never realize continuity is unhealthy until later.

## Non-Goals

- replacing the current launchd service install path immediately
- using notifications as the primary health surface
- injecting synthetic transcript messages into live provider transcripts
- changing the transport contract back to hook-time network I/O

## Research Summary

The Apple-native direction is straightforward:

- `MenuBarExtra` is the correct primitive for a persistent ambient utility in the macOS menu bar.
- A utility app that primarily lives in the menu bar is a valid product shape.
- Local notifications require explicit permission and should not be the main status surface.
- `launchd` is a legitimate background-agent model for the current CLI-installed daemon.
- `ServiceManagement` / `SMAppService` is the right future path when Longhouse becomes a bundled app that owns its helper lifecycle.

This implies a clean two-step plan:

1. **Short-term MVP:** keep the existing daemon install path, expose a stable local-health contract, and add a menu-bar-friendly status surface.
2. **Proper app path:** package the ambient surface as `Longhouse.app` first, then later let that same app own the helper lifecycle through Apple-native service management.

## Decision

Introduce an explicit **Local Agent Health Plane** with four layers:

### 1. Raw Probes

These gather facts only. No product policy.

- `engine_status_probe`
  - reads `~/.claude/engine-status.json`
  - reports age, `last_ship_at`, spool counts, dead letters, disk free, offline flag
- `service_probe`
  - reports installed/running/stopped state from launchd/systemd through existing service helpers
- `outbox_probe`
  - reports current outbox count and oldest file age
- `log_probe`
  - reports latest engine log path and mtime
- `launch_config_probe`
  - reads local Longhouse URL + machine label
  - reads local runner config when present
  - reads installed service machine-name arguments when available
  - reports whether managed-local launch inputs are coherent on this machine

### 2. Health Classifier

This converts raw probes into a small stable state model.

Stable derived fields:

- `health_state`: `healthy | degraded | broken | uninstalled`
- `severity`: `green | yellow | red | gray`
- `reasons`: list of machine-readable reason codes
- `headline`: single short human summary
- `suggested_actions`: ordered recovery actions

Important rule:

- preserve raw fields in the output
- add derived state for UI convenience
- never make the derived state the only thing available

### 3. Surfaces

Surfaces consume the same local-health contract.

- CLI human view
- CLI JSON view
- `doctor` summary/handoff
- future menu bar utility
- future web/device UI if we mirror local status back to Longhouse

### 4. Control Adapter

This is the only layer allowed to know how the helper is managed.

Short-term:

- launchd/systemd via current Python service helpers

Future:

- app-owned helper via `ServiceManagement` / `SMAppService`

The surfaces and the classifier must not care which control adapter is underneath.

## Stable Contract

The MVP must define one stable machine-readable contract that survives the launchd -> bundled-app transition.

Recommended shape:

```json
{
  "schema_version": 1,
  "collected_at": "2026-04-07T00:00:00Z",
  "health_state": "healthy",
  "severity": "green",
  "headline": "Longhouse shipping healthy",
  "reasons": [],
  "suggested_actions": [],
  "service": {
    "platform": "macos",
    "status": "running",
    "service_name": "com.longhouse.shipper",
    "service_file": "...",
    "log_path": "..."
  },
  "engine_status": {
    "exists": true,
    "fresh": true,
    "age_seconds": 4,
    "payload": {
      "version": "0.1.0",
      "daemon_pid": 123,
      "last_ship_at": "...",
      "spool_pending_count": 0,
      "spool_dead_count": 0,
      "parse_error_count_1h": 0,
      "consecutive_ship_failures": 0,
      "disk_free_bytes": 123456,
      "is_offline": false,
      "recent_dead_letters": [],
      "last_updated": "..."
    }
  },
  "outbox": {
    "file_count": 0,
    "oldest_age_seconds": null
  },
  "launch_readiness": {
    "state": "ready",
    "headline": "Managed launch configuration looks coherent",
    "reasons": [],
    "suggested_actions": [],
    "stored_url": "https://david010.longhouse.ai",
    "machine_name": "cinder",
    "service_machine_name": "cinder",
    "runner": {
      "path": "/Users/davidrose/.config/longhouse/runner.env",
      "exists": true,
      "runner_name": "cinder",
      "runner_urls": [
        "https://david010.longhouse.ai"
      ],
      "install_mode": "desktop"
    }
  },
  "thresholds": {
    "engine_fresh_seconds": 30,
    "engine_stale_seconds": 120,
    "degraded_backlog_count": 1,
    "broken_backlog_count": 25
  }
}
```

The exact thresholds may change. The schema shape should stay stable.

## Health State Rules

Initial classifier policy:

### `uninstalled` / `gray`

- service not installed
- no local engine status

This is acceptable for users who have not completed Longhouse setup yet.

### `healthy` / `green`

- service running
- engine status exists and is fresh
- no dead letters
- no consecutive ship failures
- backlog absent or negligible
- launch configuration is coherent, or not configured at all yet

### `degraded` / `yellow`

- service running but engine status is aging
- service running and engine reports offline/retrying
- service running with small or moderate backlog
- parse errors or ship failures are non-zero but not catastrophic

### `broken` / `red`

- service stopped while local work is pending
- engine status stale or missing while backlog exists
- dead letters present
- disk critically low
- backlog exceeds a clear threshold
- managed-local launch inputs disagree (URL mismatch, machine-label mismatch, or service/runtime identity mismatch)

## UX Rules

### Primary surfaces

- CLI status command
- future menu bar utility

### Secondary surfaces

- one-shot local notification on health transition to red
- startup warning banner or brief notice when the user begins a new Longhouse-managed session while the local health state is red

### Explicitly avoid

- repeated intrusive modal dialogs
- synthetic transcript messages inside Claude/Codex mid-session
- blocking a running session because remote shipping is degraded

## MVP Stages

### Stage 1: Local health contract + CLI

Goal:

- users and future UI can inspect one canonical local-health snapshot

Deliverables:

- a reusable local-health module in Python
- a machine-readable CLI output surface
- human-readable CLI output with next actions
- tests for state classification

Chosen MVP command:

- `longhouse local-health`
- `longhouse local-health --json`

### Stage 2: Faster local status writes

Goal:

- local health becomes fresh enough for ambient UX

Deliverables:

- separate local status write interval from server heartbeat interval
- keep server heartbeat coarse if desired
- refresh local engine status every few seconds instead of every five minutes

### Stage 3: Menu bar MVP

Goal:

- ambient local UX on macOS without changing the helper lifecycle yet

Deliverables:

- tiny menu bar utility
- green/yellow/red icon
- dropdown with headline, last ship time, backlog, dead letters, and actions
- actions for restart, open logs, open Longhouse, copy diagnostics

### Stage 3a: Agent Harness

Goal:

- give coding agents a fast, low-token loop for iterating on the macOS surface without depending on brittle GUI automation first

Deliverables:

- shared SwiftUI core package for the menu bar panel
- fixture-driven JSON inputs for healthy / degraded / broken states
- PNG snapshot renderer for deterministic visual output
- window-host app for normal desktop inspection
- real `MenuBarExtra` host for the actual menu bar shape
- action log output for control-surface smoke checks
- stable Make targets / scripts so future sessions can resume without re-deriving the loop

### Stage 3b: App bundle packaging

Goal:

- keep the current launchd-managed runtime path, but package the ambient surface as a real `Longhouse.app` that a human can launch directly

Deliverables:

- `Longhouse.app` installs into `/Applications`
- direct app launch shows a status or repair window instead of failing silently
- relaunching the app after background install still produces a visible status path
- launchd launches the app's inner executable for the ambient menu bar surface when Longhouse is running quietly in the background
- `longhouse local-health menubar` prefers the installed app bundle
- the window-host binary remains a debug/developer surface, not the primary installed artifact

### Stage 4: Proper app-owned helper management

Goal:

- Longhouse behaves like a native desktop product

Deliverables:

- signed app bundle
- helper bundled inside the app
- helper lifecycle managed through Apple-native service management
- same local-health contract preserved

## Integration Plan

To avoid a future rewrite, keep the code modular:

### Python modules

- `server/zerg/services/local_health.py`
  - dataclasses / typed dicts for probe output and classified snapshot
  - raw probe readers
  - classifier
- `server/zerg/cli/...`
  - thin command wrapper only
- `server/zerg/cli/doctor.py`
  - can consume the same module for summary output

### Rust engine

- continue writing `engine-status.json`
- split local status refresh cadence from remote heartbeat cadence
- keep the file as raw signal, not UI policy

### Future menu bar app

The menu bar app should depend on the **contract**, not the implementation:

- short-term: shell out to the CLI JSON surface, or re-read the same raw files and apply the same classifier rules
- longer-term: talk to a bundled helper or native adapter that emits the same schema

The menu bar app must not parse launchd output itself if the CLI already owns that knowledge.

## Harness Control Surfaces

The macOS harness should expose one obvious control surface per need:

- `make menubar-harness-test`
  - build and run Swift package tests
- `make menubar-harness-fixtures`
  - render deterministic fixture PNGs for visual review
- `make menubar-harness-live`
  - render the current machine state to a PNG from `longhouse local-health --json`
- `make menubar-harness-smoke`
  - boot both app shells, dry-run every control action, and assert action logs
- `make menubar-harness-xcuitest`
  - generate the native Xcode wrapper and run macOS XCUITests against the shared panel
- `make menubar-harness-full`
  - run the full unattended loop, including native UI tests, and write an artifact manifest
- `make menubar-harness-window`
  - launch the shared panel in a normal macOS window
- `make menubar-harness-menubar`
  - launch the actual `MenuBarExtra` shell

Important design rule:

- the snapshot renderer, window host, and menu bar host must all reuse the same shared SwiftUI view so visual drift is impossible
- unattended smoke should exercise the same action layer in `log-only` mode so boot and control wiring can be verified without destructive local side effects
- native UI automation should target the window host generated from `XcodeHarness/project.yml` so the shared panel can be exercised through XCUITest without duplicating the SwiftUI surface

Current package layout:

```text
desktop/LonghouseMenuBarHarness/
  Fixtures/
  Sources/LonghouseMenuBarCore/
  Sources/LonghouseMenuBarHarnessSnapshot/
  Sources/LonghouseMenuBarHarnessApp/
  Sources/LonghouseMenuBarHarnessMenuBar/
  XcodeHarness/
```

## User-Facing Launch Surface

The short-term product path should stay thin:

- `Longhouse.app`
  - native macOS entry point for setup, status, repair, and ambient presence
- `longhouse local-health`
  - current textual summary / JSON contract
- `longhouse local-health window`
  - launches the shared local-health panel in a normal macOS window
- `longhouse local-health menubar`
  - launches the ambient macOS `MenuBarExtra`

Important design rule:

- the Python CLI owns launch semantics and config resolution
- the Swift package owns the UI and live polling shell
- the Swift shell should accept an explicit health-command adapter so the future packaged app can swap launch/install mechanics without changing the UI contract

## Success Criteria

This effort is successful when:

- hook hot path remains local-only
- local health can be queried in one command and one JSON payload
- local status freshness is suitable for ambient UX
- users get explicit recovery guidance without transcript pollution
- a future menu bar app can be built against the existing health contract
- swapping launchd for an app-owned helper does not require a new UX/state model
- agents can iterate on the macOS UI through a deterministic snapshot/window/menubar loop without re-inventing local scripts every session
- agents can run one native XCUITest command against the shared panel without relying on AppleScript/System Events access

## Progress

### 2026-04-07

- spec created
- Stage 1 complete: `server/zerg/services/local_health.py` now defines the local-health contract and `longhouse local-health --json` exposes it for CLI and future desktop surfaces
- Stage 2 implemented in code: the daemon now refreshes the local status file on a short cadence while keeping server heartbeat coarse
- Stage 3a harness landed: a Swift package under `desktop/LonghouseMenuBarHarness/` now provides shared UI, fixture/live snapshot rendering, a window host, a real menu bar host, and stable Make/script entrypoints
- Stage 3a harness now has an unattended smoke path and `manifest.json` artifact output, so agents can run one command and inspect one directory instead of replaying manual shell steps
- Stage 3b implemented: `XcodeHarness/project.yml` now generates a native wrapper app plus XCUITest target, and the harness exposes that as `make menubar-harness-xcuitest`
- `make menubar-harness-full` now covers fixture rendering, live rendering, shell smoke, native UI tests, and one artifact directory for screenshots/logs/result bundles
- Stage 3c implemented: `longhouse local-health window` and `longhouse local-health menubar` now launch the shared SwiftUI surface through the existing local-health contract, using an explicit health-command adapter instead of hard-coding launchd knowledge into the app

## References

- `engine/src/heartbeat.rs`
- `engine/src/daemon.rs`
- `engine/src/outbox.rs`
- `server/zerg/services/shipper/service.py`
- `server/zerg/cli/doctor.py`
- Apple Developer Documentation: `MenuBarExtra`
- Apple Developer Documentation: `ServiceManagement` / `SMAppService`
- Apple Human Interface Guidelines: Notifications
- Apple documentation archive: `Creating Launchd Jobs`
