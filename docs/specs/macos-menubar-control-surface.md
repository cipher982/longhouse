# macOS Menu Bar Control Surface

Status: Active
Owner: desktop launch surface
Updated: 2026-04-13

## Problem

The current menu bar surface fails in two directions at once:

- the host interaction still feels fragile under rapid toggling
- the healthy-state panel is too sparse to justify existing as ambient mission control
- the visual language reads like a web card dropped into macOS instead of a native utility surface

This spec replaces the current "status sheet" shape with a denser, native-feeling control surface that stays truthful to the local machine signals we can collect cheaply.

## Audit Findings

- The open path must remain effectively instant. No synchronous health refresh or heavy local probing can happen on click.
- The current `local-health` payload is strong on repair signals but weak on positive ambient telemetry.
- The local shipper DB (`~/.claude/longhouse-shipper.db`) already exposes truthful session-level activity we can use for a healthy-state "Today" view:
  - distinct sessions touched today
  - sessions touched recently
  - provider mix for recently active sessions
- The shipper DB does not by itself support an honest "messages today" metric. Do not guess from `file_state`.
- The menu bar icon comes from the canonical master-logo pipeline. Preserve that pipeline and fix presentation/sizing rather than hand-authoring a new icon.

## Goals

- Render immediately from cached state.
- Make the healthy 99% case feel useful, not empty.
- Use structured telemetry, not explanatory paragraphs, as the default visual language.
- Keep repair verbs and diagnostics available without spending prime healthy-state real estate on them.
- Feel native to modern macOS: restrained controls, system typography, material-backed chrome, minimal color.
- Keep icon silhouette, padding, and detail stable.

## Chosen Direction

Build a compact telemetry control surface with a `Now` section, a `Today` section, and one lightweight trend surface.

This is a hybrid of:

- `Telemetry Strip + Grid` for core machine health
- `Now / Today Split` for proving the machine is doing useful work, not merely "green"

Healthy-state hierarchy is now explicitly:

1. headline + status + small accessory controls
2. one compact metric strip for the four numbers that matter at a glance
3. one dense operations table with right-aligned values
4. one lightweight trend zone
5. one primary exit action, with diagnostics behind a secondary menu

## Healthy State

The healthy/default state should show:

1. Header
   - brand icon
   - headline
   - status badge
   - snapshot age
   - refresh affordance
2. `Now`
   - last ship
   - launch readiness
   - engine freshness
   - queue / outbox / dead counts as compact telemetry, not big cards
   - disk free only when it is approaching thresholds
3. `Today`
   - sessions touched today
   - active now
   - provider mix
4. `Pulse`
   - one compact chart driven by cached snapshot history in the running app
   - phase 1 target: ship cadence / freshness pulse
5. Actions
   - primary: `Open Longhouse`
   - secondary: `Details`
   - refresh stays icon-only

Healthy-state layout rules:

- fixed height, no scroll
- no multi-line paragraphs
- no duplicated primary actions
- no individual metric cards unless they meaningfully improve scanning
- values right-aligned and preferably monospaced
- healthy-state secondary verbs stay in a menu, not a visible button wall
- the surface should read like a compact utility console, not a dashboard webpage

Healthy state must not show:

- `Doctor`, `Repair`, `Logs`, or `Copy JSON` as always-visible primary controls
- large zero-value tiles
- long prose summaries unless the machine is not green

## Attention / Broken State

Attention state becomes `fix first, evidence second`.

Visible by default:

- strongest headline
- one primary `Repair` action
- the top 2-4 blocking signals
- concise next steps

Progressive disclosure:

- logs
- copy diagnostics
- raw config / runner details

## Data Contract

Phase 1 adds a machine-local ambient activity summary to `local-health`:

- `sessions_today`
- `sessions_recent`
- `provider_counts_today`
- `latest_activity_at`

Phase 1 does not add:

- `messages_today`
- fake hourly activity charts derived from `file_state` alone

If we later want message counts or true activity timelines, add a dedicated background collector or ledger first.

## Performance Rules

- Click/open must never wait on `longhouse local-health --json`.
- Any activity aggregation must run in the existing background refresh path only.
- Trend charts must use cached in-memory snapshot history, not extra subprocesses on open.
- Healthy-state layout must have deterministic sizing and no scrolling.

## Refresh Architecture

Sampling and presentation must be treated as separate loops.

- `Health sampling` is the slower machine-truth loop. It reads local state and produces a new snapshot.
- `Presentation ticking` is the cheap UI loop. It only advances relative labels such as `Updated`, `Ship`, and `Heartbeat` while the panel is visible.

Rules:

- Opening the panel must never trigger or wait on a full health sample.
- Background polling must stay silent in the visible UI unless the machine state materially changes.
- Manual refresh is a user verb, not the same thing as background polling. It may show a subtle inline spinner, but not a toast or full-panel loading state.
- `Updated` must advance locally between samples instead of staying frozen until the next snapshot lands.
- Normal healthy-state refresh must not look like a page reload.

## Success Criteria

- `Updated` increments while the panel is open even if no new sample has landed yet.
- Background polling does not disable the refresh control or show a toast/banner.
- Manual refresh shows only a small inline affordance and never collapses the panel into a loading surface when a cached snapshot exists.
- If a background poll is in flight and the user clicks refresh, the manual request is not silently lost.
- Rapid open/close toggle latency stays in the current low-hundreds-of-milliseconds range on the installed app.
- Fixture, live render, harness smoke, packaging smoke, and installed-bundle verification all pass after the change.

## Visual Rules

- Prefer system text styles and monospaced digits for telemetry values.
- Use material-backed or AppKit-native chrome instead of stacked "card inside card" containers.
- Keep color discipline tight: one state accent plus exception colors only when signals are abnormal.
- Reserve visual emphasis for telemetry changes, not for buttons.
- Prefer one unified strip or section shell over repeated inset rounded cards in the healthy path.
- Keep SF Symbols sparse: status glyph plus small action glyphs only.

## Icon Rules

- Keep the menu bar icon sourced from the canonical SVG generation pipeline.
- Add a repeatable verification step for the installed menu bar glyph so detail/padding regressions are caught before ship.
- Fix sizing/scaling/presentation in the status item host before touching source art.

## Validation

- fixture snapshots for healthy, degraded, and broken states
- menubar/window smoke
- live installed-app reinstall and reload
- real menu bar toggle verification on the installed app
