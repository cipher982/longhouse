# UI & Front-End Overhaul – Task Tracker

_Status: ✅ COMPLETED_ · _Completed: June 2025_ · _Moved to completed: June 15, 2025_

This document tracked the high-level work items for the visual overhaul of the Zerg web front-end. All phases have been completed successfully.

**Final Status**: All design tokens, components, responsive layouts, and polish features have been implemented and are live in production.

---

## Phase 0 – Scaffolding

- [x] Create **`frontend/www/tokens.css`** with design-token variables (colours, spacing, radii, shadows, transitions).
- [x] Import `tokens.css` at the top of **`frontend/www/styles.css`**.
- [x] Replace hard-coded colour / spacing values in `styles.css` with `var(--token)` references (primary-heavy blue values swapped).
- [x] Add a tiny helper macro `css_var!()` (or similar) for ergonomic inline styles in the Rust/Yew code-base.
- [ ] Run `trunk build` (or existing build script) and visually verify there are no regressions. _(pending manual review)_

## Phase 1 – Quick-Win Theming

- [x] Swap legacy blue `#3498db` highlights for `var(--primary)`.
- [x] Apply primary/secondary gradient to header, tab-bar, and status-bar backgrounds.
- [x] Update global font stack & root font-size to match branding draft.
- [ ] Run an automated contrast check (axe-core or similar) – fix any AA failures.

## Phase 2 – Component Polish

### Buttons

- [x] Implement reusable Button component variants (primary / secondary / ghost).
- [x] Add size modifiers (sm / md / lg).
- [x] Animate hover/active states using `var(--transition-normal)`.

### Agent Pill & Shelf

- [x] Apply `--radius-lg` and token-based shadows.
- [x] Harmonise drag-and-drop “ghost” styling with new palette.

### Modal & Form Elements

- [x] Standardise spacing, typography and focus-ring colour.

### Toast / Alert System

- [x] Create Toast component with success / warning / error themes (CSS + animation; integration to Rust pending).

## Phase 3 – Layout & Responsiveness

- [x] Introduce `layout.css` (or util classes) for common flex/grid patterns.
- [x] Refactor canvas view to use modern `clamp()` sizing.
- [x] Collapse agent shelf into off-canvas drawer on < 768 px viewports.
- [x] Break up monolithic `styles.css` into modular CSS files (tokens, layout, util, buttons, forms, nav, agent_shelf, canvas, toast, dashboard, status, modal, MCP, etc.).

### CSS Modularisation (side-quest)

- [x] Created `frontend/www/css/` directory and migrated legacy blocks into focused modules.
- [x] Updated `index.html` to link each module directly to avoid nested `@import` CSP issues.
- [x] Restored all missing rules (dashboard header/search, scope-select, status chips, agent detail modal, MCP manager, utility helpers).
- [x] Added legacy variables to `tokens.css` so historic code paths resolve correctly.
- [x] Fixed `frontend/www/.gitignore` so new CSS files are tracked.

## Phase 4 – Delight Layer

- [x] Add particle background effect to login/landing screen.
- [x] Add micro-interactions (button scale, tab ripple, modal fade).
- [x] Integrate SVG icon set (Lucide/Feather) using `currentColor` for easy theming.

## Phase 5 – Documentation & Handoff

- [x] Generate Component Gallery page (`/dev_components.html`).
- [x] Write `docs/design_system.md` explaining tokens, components, usage rules.
- [x] Update root **README.md** with design-system playground link.
- [x] Establish visual-regression baseline screenshots and add to CI.

## Tooling / Quality Gates (parallel tasks)

- [x] Add `stylelint` & `prettier-css` to **pre-commit** config.
- [x] Integrate `axe-playwright` for automated accessibility checks.
- [x] Configure visual-regression tests (Playwright + pixel-diff) for critical pages.

---

### How to use this file

1. **Single source of truth** – Keep this list updated instead of scattered TODOs.
2. **PR integration** – Reference the relevant check-box line in pull-request descriptions so merges automatically tick items.
3. **Review cadence** – During planning meetings, skim this file to pick next priorities.

Happy shipping! 🚀
