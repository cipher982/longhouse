# Managed Codex Prestarted TUI Attach

Status: Planned
Owner: Machine Agent + managed Codex bridge
Updated: 2026-05-26
Related: `managed-codex-state-compat.md`, `managed-codex-liveness.md`, `remote-session-launch.md`

## Problem

Codex CLI 0.133 changed fresh remote TUI startup so the visible TUI is created
before `thread/start` has completed. During that pre-thread window, internal
startup events can route through Codex's active-thread-only path and the TUI can
print `No active thread is available.` before the thread is installed.

That message is an upstream startup race, not a Longhouse control failure, but
it appears exactly where users expect Longhouse managed Codex to be healthy. The
Longhouse wrapper should avoid the fragile upstream fresh-start path while still
using the user's stock upstream `codex` binary.

## Decision

Local `longhouse codex` should create the initial Codex thread through the
Longhouse bridge before launching the visible TUI. The TUI should then attach to
that known thread with Codex's existing resume path:

```text
codex resume <thread_id> --enable tui_app_server --remote <bridge_ws_url>
```

This uses only upstream Codex protocol and CLI surfaces. Longhouse does not
vendor, patch, pin, or replace Codex.

## Contract

Thread creation and lifecycle mode are separate axes:

- **Initial thread creation** answers who calls `thread/start`.
- **Launch mode** answers how the bridge should be treated by liveness and
  reaping code.

Local TUI-attached managed Codex must use:

```text
create_initial_thread=true
launch_mode=tui
```

Detached-UI remote launch must use:

```text
create_initial_thread=true
launch_mode=detached-ui
```

Legacy TUI startup where the TUI creates the thread remains representable as:

```text
create_initial_thread=false
launch_mode=tui
```

Detached-UI remote launch is explicit: `create_initial_thread=true` plus
`launch_mode=detached-ui`. Do not add compatibility flags that collapse these
axes again.

## Implementation Plan

1. Add an explicit bridge option for prestarting the initial thread without
   implying detached-UI lifecycle, for example `--create-initial-thread`.
2. Rename the Rust config axis to `create_initial_thread` and add an explicit
   launch-mode enum for `tui` vs detached-UI lifecycle. Consolidate persisted
   launch-mode string mapping in one helper.
3. Update remote launch to pass detached-UI lifecycle explicitly while keeping
   its existing prestarted-thread behavior.
4. Change `longhouse codex` to start the bridge with the new prestart option.
5. When local prestart is requested, `ready` without `thread_id` is a launch
   failure. The CLI must fail before starting a visible TUI.
6. When the bridge start summary includes `thread_id`, launch and print attach
   commands with `resume <thread_id>`.
7. Keep bridge cleanup and signal handling unchanged; local TUI-attached
   sessions still persist `launch_mode=tui`.

The bridge state schema version is intentionally unchanged. On-disk field names
and accepted launch-mode values do not change.

## Tests

- Python CLI tests assert `longhouse codex` asks the engine to prestart a thread.
- Python CLI tests assert auto-attach and printed attach commands include
  `resume <thread_id>` when the bridge returns one.
- Python CLI tests assert prestart mode fails fast if the bridge reports ready
  without a thread id.
- Rust CLI tests assert `--create-initial-thread` and `--launch-mode` keep
  initial thread creation separate from lifecycle.
- Rust bridge tests assert prestarted TUI state persists `launch_mode=tui`.
- Reaper tests cover a prestarted TUI bridge with no TUI attachment during and
  after the grace window.
- Detached-UI tests assert new bridge writers persist `launch_mode=detached_ui`;
  readers still tolerate older dogfood `headless` state.
