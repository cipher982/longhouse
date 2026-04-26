# Session Runtime Display Contract

Status: Active buildout
Last updated: 2026-04-26

## Goal

Make live session status a server-derived contract instead of letting web,
iOS, widgets, and future clients each re-derive runtime semantics.

The raw truth still comes from `SessionRuntimeState`. The display contract is a
presentation-ready projection attached to session responses as
`runtime_display`.

## Why

The web session detail page has a persistent runtime strip. The iOS session
view had the same raw fields, but its live state was only visible in the
scrollable header. With the keyboard open, an active managed session could look
idle until the next tool row appeared.

That is not an iOS-only layout issue. It is a drift issue:

- server knows runtime phase, freshness, tool, and capabilities
- web derives labels, tones, and "working vs waiting" state
- iOS derives a different, smaller version

The product needs one derived runtime display shape.

## Contract

Every session response may include:

```json
{
  "runtime_display": {
    "truth_tier": "managed-local",
    "state": "running",
    "tone": "running",
    "headline": "Working",
    "detail": "Running Shell",
    "phase_label": "Running Shell",
    "compact_tool_label": "Shell",
    "is_live": true,
    "is_executing": true,
    "needs_attention": false,
    "is_idle": false,
    "heuristic_active": false,
    "is_managed_local_truth": true,
    "has_signal": true
  }
}
```

`runtime_display` is derived from the same backend overlay that populates
`presence_state`, `presence_tool`, `display_phase`, `confidence`, and
`capabilities`. Those raw fields remain for API compatibility and low-level
debugging, but client UI should prefer `runtime_display`.

## Rules

- Backend owns semantic display state: `truth_tier`, `tone`, `headline`,
  `detail`, booleans, and compact tool labels.
- Clients may format layout, colors, and relative times locally.
- Clients may keep fallback derivation for older payloads, but fallback logic is
  compatibility code, not a second source of truth.
- `managed`, `live`, `reattachable`, `running`, and `needs_attention` remain
  separate axes. Do not collapse them into a single status string.
- If web and iOS need a new runtime label or state, add it to
  `runtime_display` first.

## Client Expectations

- Web: `SessionRuntimeStrip`, timeline cards, and workspace refresh decisions
  should consume `runtime_display` when available.
- iOS: the in-app session view should keep a persistent runtime strip near the
  composer, so active managed sessions show visible progress even when the
  scroll header is offscreen.
- Widgets and Live Activities may continue using their existing payloads, but
  should converge on this contract when they need richer state.
