# Managed Codex Menu Bar Presence

## Problem

The macOS menu bar can show many Codex managed sessions as blue
`attached` rows even when the user expects closed windows to disappear.
Investigation showed that most rows are not stale database artifacts:
they correspond to live local Codex bridge, app-server, wrapper, or
remote TUI processes.

The defect is semantic collapse. Longhouse uses `attached` to mean a
managed control path is available, while the menu bar reads it as a
visible terminal or window being attached. Detached-ui Codex sessions are
valid background control sessions, but the menu bar presents them like
foreground terminal sessions and does not offer a stop action because
their normalized state is `attached`.

## Constraints

- Runtime Host lease semantics stay unchanged. A detached-ui Codex bridge
  with a ready app server and thread remains `state=attached` because it
  is remotely steerable.
- The reaper must keep refusing to kill foreground TUI sessions while a
  remote TUI process is still attached.
- The menu bar must tolerate mixed versions where older engines do not
  emit the new fields.
- This change does not define an idle TTL for detached-ui sessions.
  Background session lifecycle policy is a separate product decision.

## Design

Add an engine-owned local presentation field:

```text
ui_presence = foreground_tui | background | detached | degraded
```

The engine computes `ui_presence` from the same Codex bridge observation
used for heartbeat leasing:

- `foreground_tui`: launch mode is `tui` and a remote TUI attachment is
  currently observed.
- `background`: launch mode is `detached_ui` and the bridge/app-server
  control path is ready.
- `degraded`: a managed bridge exists but the control path is not healthy
  enough to be considered attached.
- `detached`: no live bridge/control path is available.

The engine also carries raw debug metadata (`launch_mode`,
`ui_attached`) through the resolved bridge payload. Server/local-health
passes these fields through in both fast engine-status and deep fallback
paths.

The menu bar uses `ui_presence` for human-facing grouping and actions:

- Foreground TUI rows are labeled as terminal-attached and do not show a
  stop button.
- Background rows are labeled as background managed sessions and show a
  stop action.
- Detached, degraded, and orphan bridge rows keep stop affordances.
- Existing state counts remain available for compatibility, but the panel
  header favors foreground/background/detached/degraded counts when
  `ui_presence` exists.

## Verification

- Rust tests assert detached-ui stays `state=attached` while emitting
  `ui_presence=background`, and TUI attachments emit
  `ui_presence=foreground_tui`.
- Python local-health tests cover fast engine-status passthrough, old
  engine missing-field fallback, and deep bridge scanning.
- Swift tests cover decoding with and without new fields, summary labels,
  and stop-action availability.
- Menu bar harness renders a mixed foreground/background/degraded fixture
  for visual smoke.
