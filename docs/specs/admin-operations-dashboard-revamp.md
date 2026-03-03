# Admin Operations Dashboard Revamp (2026-03-03)

## Why This Exists

The current Admin Operations Dashboard violates a basic trust requirement: labels and data windows must match.

Current behavior:
- The UI offers `Today`, `Last 7 Days`, `Last 30 Days`.
- The summary API ignores that selection and always returns today-scoped keys (`runs_today`, `cost_today_usd`, `top_fiches_today`).
- Users see contradictory copy such as `Last 30 Days` with `Runs Today`.

This document defines the semantic and UX reset.

## Problems To Fix

1. Time-window mismatch
- Window selector is local state only and not included in `/api/ops/summary` requests.
- Metrics imply one window while the values come from another.

2. Metric semantics are mixed but unlabeled
- Some metrics are intentionally fixed-window (`errors_last_hour`, `active_users_24h`, daily budgets).
- Other metrics should be window-scoped (runs, cost, latency, top fiches).
- The page does not communicate that difference.

3. Styling quality/regression
- `profile-admin.css` contains malformed/nested admin blocks that are hard to reason about and have drifted over time.
- Admin demo cards rely on inline styles, bypassing shared tokens and consistency.

## Product Principles (First Principles)

1. Truth over cleverness
- Titles and subtitles must always describe the exact aggregation window.

2. One control, deterministic effect
- Changing time window must refetch summary and update all window-scoped sections.

3. Explicit mixed windows
- Fixed-window metrics stay fixed, but are visibly labeled as such.

4. Visual hierarchy
- Fast scan first: key KPIs at top.
- Drilldown second: top fiches and user usage.
- Operations/admin actions last.

5. Mobile and desktop parity
- No hidden controls.
- KPI cards reflow cleanly from 4-up/3-up to single column.

## API Contract Changes

Endpoint: `GET /api/ops/summary?window=today|7d|30d`

Response fields (canonical):
- `window`: selected key (`today`, `7d`, `30d`)
- `window_label`: user-facing label (`Today`, `Last 7 Days`, `Last 30 Days`)
- `runs`: total runs in selected window
- `cost_usd`: total known cost in selected window
- `top_fiches`: ranked fiche aggregates in selected window
- `latency_ms`: p50/p95 for successful runs in selected window
- `budget_user`, `budget_global`: always daily budget state (today)
- `errors_last_hour`: always last 60 minutes
- `active_users_24h`: always trailing 24 hours
- `fiches_total`, `fiches_scheduled`: current inventory

Compatibility fields retained during transition:
- `runs_today`, `cost_today_usd`, `top_fiches_today` (mirrors canonical values for now)

## UI Contract Changes

1. Window selector drives query key + API param
- Query key: `['ops-summary', selectedWindow]`.
- Request: `/api/ops/summary?window=<selectedWindow>`.

2. KPI card copy
- `Runs (Window)` with subtitle `In <window_label>`.
- `Cost (Window)` with subtitle `In <window_label>`.
- `Latency P95 (Window)` with subtitle `P50 in <window_label>`.
- Keep fixed metrics explicit:
  - `Errors (1h)`
  - `User Budget (today)`
  - `Global Budget (today)`

3. Top fiches section
- Title: `Top Performing Fiches (<window_label>)`.

4. Styling cleanup
- Remove inline styles from demo account cards and button.
- Use scoped admin classes + existing design tokens.
- Add a concise metric semantics note block near KPI grid.

## Testing Plan

Backend (`tests_lite`):
- Summary endpoint honors `window` and returns different run/cost totals by window.
- Summary response echoes `window` and `window_label`.

Frontend (`AdminPage.test.tsx`):
- Initial render uses default window request.
- Changing selector to `7d` triggers request with `window=7d`.
- Window-scoped labels update to selected window.

Full verification:
- `make test`
- `make test-e2e`
- Post-deploy: `make qa-live`

## Done Criteria

- No contradictory wording (`Last 30 Days` + `Runs Today`) anywhere on Admin.
- Window selector materially changes data for window-scoped metrics.
- Fixed-window metrics are clearly labeled as fixed.
- Admin page styles are valid, scoped, and free of inline card layout overrides.
