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
- [x] Add `Spinner` UI primitive; replace inline spinner sizing in EmptyState loaders.
- [x] Knowledge Sources: replace inline header actions styling with CSS class.
- [x] Runner detail page: adopt PageShell + SectionHeader + standardized Button usage.
- [x] Modal actions: migrate Add Runner / Add Context / Add Knowledge Source buttons to `Button` + `Spinner`.
- [x] Integrations + Knowledge Sources: adopt PageShell for consistent width/padding.
- [x] Consolidate header/section patterns (SectionHeader everywhere).
- [x] Info pages: remove inline styles from Docs/Changelog.
- [x] Dashboard: swap create-agent loading spinner to shared `Spinner`.

## Phase 2 — Styling & Tokens
- [ ] Audit token usage (replace raw colors/spacing with tokens).
- [ ] Reduce legacy CSS overrides; move to layered, scoped styles.
- [ ] Normalize type scale and heading rhythm across pages.

## Phase 3 — Automated UI QA
- [ ] Add baseline visual snapshots for key pages (dashboard/chat/canvas/settings).
- [ ] Add a focused mobile viewport test project in Playwright (after design lock).
- [ ] Add a11y regression gate (axe + color contrast) to CI.

## Phase 4 — Mobile Responsiveness (after desktop is locked)
- [ ] Define breakpoints and layout rules per page.
- [ ] Mobile nav: refine drawer + header behavior.
- [ ] Chat layout: sidebar behavior + composer sizing.
- [ ] Canvas: layout rules + minimal mode.
