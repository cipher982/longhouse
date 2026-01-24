# UI Audit + Refactor Task List (Swarmlet Web)

Last updated: 2026-01-23
Owner: David Rose

Principle: this doc is the source of truth for UI work. Update after every meaningful change.

## Status Key
- [ ] pending
- [~] in progress
- [x] done
- [!] blocked

## Phase 0 — Setup & QA
- [x] Create this task doc.
- [x] Add quick UI QA targets (`make qa-ui`, `make qa-ui-visual`).
- [x] Document UI QA workflow in `docs/TESTING.md`.

## Phase 1 — Structure & Consistency (Desktop Lock)
- [x] Centralize navigation items (single source shared by header + mobile drawer).
- [x] Introduce page shell/layout primitives (max width, padding, title/description).
- [~] Remove inline layout styles in Settings/Profile/Admin/TraceExplorer.
  - [x] Settings page: replace inline layout styles with scoped classes.
  - [x] Profile page: replace inline layout styles with scoped classes.
  - [x] Runners page: replace inline layout styles with scoped classes.
  - [x] Admin page: replace inline layout styles with scoped classes (metrics, tables, dev tools).
  - [x] Reliability page: replace inline layout styles with scoped classes.
  - [x] Trace Explorer page: replace inline layout styles with scoped classes.
- [x] Replace legacy button usage in active pages with `components/ui/Button`.
  - [x] Contacts page: swapped legacy `.btn-*` usage to `Button` component.
  - [x] Pricing page: swapped legacy CTA button to `Button` component.
  - [x] Connector config modal: swapped legacy `.btn-*` usage to `Button` component.
  - [x] Connector credentials panel: swapped legacy `.btn-*` usage to `Button` component.
  - [x] Agent settings drawer: swapped legacy `.btn-*` usage to `Button` component (incl. custom tool action).
  - [x] Landing hero/HowItWorks/Pricing/Footer: swapped CTA buttons to `Button` component.
  - [x] Knowledge source card actions: swapped legacy buttons to `Button` component.
- [x] Add `Spinner` UI primitive; replace inline spinner sizing in EmptyState loaders.
- [x] Knowledge Sources: replace inline header actions styling with CSS class.
- [x] Runner detail page: adopt PageShell + SectionHeader + standardized Button usage.
- [x] Modal actions: migrate Add Runner / Add Context / Add Knowledge Source buttons to `Button` + `Spinner`.
- [x] Integrations + Knowledge Sources: adopt PageShell for consistent width/padding.
- [x] Consolidate header/section patterns (SectionHeader everywhere).
- [x] Info pages: remove inline styles from Docs/Changelog.
- [x] Dashboard: swap create-agent loading spinner to shared `Spinner`.
- [x] App route loader: use shared `Spinner`.
- [x] Dashboard empty-state logo: move inline sizing to CSS.
- [x] Jarvis chat loading state: move inline styles into jarvis CSS.
- [x] Landing perf HUD: move inline styles into landing CSS.

## Phase 2 — Styling & Tokens
- [x] Audit token usage (replace raw colors/spacing with tokens).
  - [x] Execution log stream: keep explicit neon palette (intentional cyberpunk look).
- [~] Reduce legacy CSS overrides; move to layered, scoped styles.
  - [x] Removed unused legacy `.btn-*` overrides from `profile-admin.css`.
  - [x] Removed unused legacy `.btn-*` styles from `settings.css`.
  - [x] Runner detail: replace hard-coded colors with design tokens.
  - [x] Knowledge Sources: replace hard-coded colors with design tokens.
  - [x] Runners page: replace hard-coded colors with design tokens.
  - [x] Dashboard + Trace Explorer + Reliability: replace warning/error hex values with tokens.
  - [x] Settings + Profile/Admin: replace status/error hex values with tokens.
  - [x] Confirm dialog: replace warning/danger hover hex values with tokens.
  - [x] Info pages: replace changelog badge hex colors with tokens.
  - [x] Execution results: replace status badge hex colors with tokens.
  - [x] UI error banners + Trace Explorer anomaly accents: replace hex values with tokens.
  - [x] Canvas controls + run button: replace disabled/toggle/status hex values with tokens.
  - [x] Profile/Admin upload button: replace gradient hex values with tokens.
  - [x] Tool config modal: replace light theme hex colors with tokens.
  - [x] Chat code blocks: replace background hex with token.
  - [x] Ops HUD: replace text/border hex values with tokens.
- [x] UI + legacy buttons: replace hard-coded white text with tokens.
- [x] Reliability + Trace Explorer: replace inline status/source hex values with tokens.
- [x] Admin metrics: replace inline metric hex values with tokens.
- [x] Normalize type scale and heading rhythm across pages.
  - [x] Added shared `ui-section-title` / `ui-subsection-title` styles and applied across app pages.

## Phase 3 — Automated UI QA
- [x] Add baseline visual snapshots for key pages (dashboard/chat/canvas/settings).
  - [x] Added public-page baseline spec (`ui_baseline_public.spec.ts`).
  - [x] Added app-page baseline spec (`ui_baseline_app.spec.ts`) with core + settings/runners/admin/traces/reliability pages.
- [x] Baseline specs use deterministic query flags (`clock`, `effects=off`, `seed`).
- [x] Add Makefile helpers for baseline runs (`qa-ui-baseline`, `qa-ui-baseline-update`).
- [x] Document snapshot update tip (`PWUPDATE=1`) in testing guide.
- [x] Add `qa-ui-full` Makefile helper for a one-command UI regression sweep.
- [x] Settings page: add `data-ready` signal to align readiness contract for QA.
- [x] Profile/Admin pages: add `data-ready` signals to align readiness contract for QA.
- [x] Reliability/Trace Explorer pages: add `data-ready` signals to align readiness contract for QA.
- [x] Readiness contract doc updated with new page coverage.
- [x] Add a focused mobile viewport test project in Playwright (after design lock).
  - [x] Added `mobile` Playwright project + baseline spec (`tests/mobile/ui_baseline_mobile.spec.ts`).
  - [x] Added Makefile helpers for mobile baseline runs.
  - [x] Expanded mobile baseline coverage (profile/admin/traces/reliability).
  - [x] Added mobile nav-open baseline for dashboard.
  - [x] Added small-screen mobile project (`iPhone SE`) for baseline coverage.
- [!] Add a11y regression gate (axe + color contrast) to CI.
  - [!] Blocked: CI doesn’t install Playwright + backend deps (`uv`, Postgres) yet.

## Phase 4 — Mobile Responsiveness (after desktop is locked)
- [~] Define breakpoints and layout rules per page.
  - [x] Added mobile stacking for shared `SectionHeader` actions at <=768px.
  - [x] Runner detail sections: reduce padding + stack grids on mobile.
- [x] Mobile nav: refine drawer + header behavior.
  - [x] Added safe-area padding for mobile header + nav drawer.
  - [x] Tightened header control sizing for narrow screens.
  - [x] Added safe-area padding for status bar on mobile.
  - [x] Added iOS scroll smoothing and overscroll containment for nav drawer.
  - [x] Deduped mobile drawer user identity display (no double email).
- [x] Chat layout: sidebar behavior + composer sizing.
  - [x] Added mobile thread scrim + tightened composer layout and safe-area spacing.
  - [x] Updated thread sidebar to slide with transform + responsive width.
  - [x] Added iOS scroll smoothing for threads/messages.
  - [x] Added mobile thread toggle button in chat header.
- [x] Canvas: layout rules + minimal mode.
  - [x] Mobile logs panel: bottom sheet layout + safe-area padding.
  - [x] Added mobile minimal mode toggle (hides snap/guides + tightens controls).
