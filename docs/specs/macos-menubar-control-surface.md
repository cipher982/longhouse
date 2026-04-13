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
- Optimize for recognition before density. The user should not have to decode our internal model to understand the panel.
- Keep repair verbs and diagnostics available without spending prime healthy-state real estate on them.
- Feel native to modern macOS: restrained controls, system typography, material-backed chrome, minimal color.
- Keep icon silhouette, padding, and detail stable.

## Chosen Direction

Build a compact mission console organized around the user's first-open questions, not around our internal telemetry buckets.

The healthy-state scan order is explicitly:

1. Is shipping healthy?
2. Is it doing work right now?
3. Is anything backing up?
4. Where is it connected?
5. What recent activity proves it is alive?

Healthy-state hierarchy is now explicitly:

1. headline + status + small accessory controls
2. one primary `Right now` board with the four highest-signal answers
3. one `Recent activity` feed that reads like live motion, not a chart to decode
4. one `Today` provider distribution section
5. one compact `Connected` summary
6. one primary exit action, with diagnostics behind a secondary menu

## Healthy State

The healthy/default state should show:

1. Header
   - brand icon
   - headline
   - status badge
   - snapshot age
   - refresh affordance
2. `Right now`
   - last ship
   - active now
   - sessions today
   - queue state
   - one short support line for launch readiness, engine freshness, and disk free
3. `Recent activity`
   - the most recent distinct session touches, shown as a live feed with provider + age
   - when local metadata allows it, rows should lead with workspace/repo context, not provider-only labels
   - recent activity is allowed to look busy, but it must read immediately as time-ordered activity
4. `Today`
   - provider mix
   - total sessions touched today
   - recent provider mix only if it helps scanning, not as a second chart to decode
5. `Connected`
   - launch target
   - runtime mode
   - host / app identity
6. Actions
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
- user-facing labels should be plain language, not themed control-room jargon
- no section may require learning our bucket definitions to be useful
- charts and bars are only allowed when the unit and meaning are obvious on first read

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
- `provider_counts_recent`
- `latest_activity_at`
- `session_recency_bands`
- `recent_touches`

Phase 1 does not add:

- `messages_today`
- fake hourly activity charts derived from `file_state` alone

If we later want message counts or true activity timelines, add a dedicated background collector or ledger first.

## Performance Rules

- Click/open must never wait on `longhouse local-health --json`.
- Any activity aggregation must run in the existing background refresh path only.
- Trend charts must use cached in-memory snapshot history, not extra subprocesses on open.
- Trend charts must only render when they carry real variance or a second contrasting signal. Flat windows should collapse to an explicit steady-state summary.
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
- Theme should come from rhythm, typography, and motion cues, not from obscure labels like `rail`, `ledger`, or other control-room metaphors.

## Icon Rules

- Keep the menu bar icon sourced from the canonical SVG generation pipeline.
- Add a repeatable verification step for the installed menu bar glyph so detail/padding regressions are caught before ship.
- Fix sizing/scaling/presentation in the status item host before touching source art.

## Validation

- fixture snapshots for healthy, degraded, and broken states
- menubar/window smoke
- live installed-app reinstall and reload
- real menu bar toggle verification on the installed app
