# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---

# Jarvis Look-and-Feel → Swarmlet UI System (Spec + Roadmap)

**Date:** 2025-12-22
**Status:** Completed

**Update (2025-12-22):** Background animations removed for GPU efficiency. Dashboard/app pages now use static grid + nebula gradients with glass panels (backdrop-filter). Landing page retains animated effects.

## Why this exists
Jarvis chat (`/chat`) has a cohesive, “premium” cyber/glass aesthetic (depth, glow, motion, typography). The rest of the Swarmlet SPA (dashboard + settings + runners + admin + canvas) is a mix of:
- legacy global CSS
- one-off page CSS
- inconsistent tokens / hardcoded colors

Goal: capture what makes Jarvis feel “done”, distill it into a shared UI system, then roll that style across the rest of the app without copying Jarvis CSS wholesale.

## Target outcome (plain English)
1) The whole app looks like one product (not “Jarvis vs dashboard”).
2) The default aesthetic is “Jarvis-ish”: glassy surfaces, crisp typography, subtle glow + motion.
3) The “heavy FX” (animated grids/nebula/reactor/ambient loops) are toggleable via one build-time env var.

## Source of truth (repo reality)
**Jarvis styling + theme**
- `apps/zerg/frontend-web/src/jarvis/styles/index.css`
- `apps/zerg/frontend-web/src/jarvis/styles/base.css`
- `apps/zerg/frontend-web/src/jarvis/styles/layout.css`
- `apps/zerg/frontend-web/src/jarvis/styles/chat.css`
- `apps/zerg/frontend-web/src/jarvis/styles/sidebar.css`
- `apps/zerg/frontend-web/src/jarvis/styles/voice-button.css`
- `apps/zerg/frontend-web/src/jarvis/styles/animations.css`
- `apps/zerg/frontend-web/src/styles/tokens.css` (Jarvis “glass” theme via `.jarvis-container`)

**Dashboard + other pages**
- `apps/zerg/frontend-web/src/pages/DashboardPage.tsx`
- `apps/zerg/frontend-web/src/styles/css/dashboard.css` (legacy layer)
- `apps/zerg/frontend-web/src/styles/css/forms.css` (legacy layer)
- `apps/zerg/frontend-web/src/styles/runners.css`, `apps/zerg/frontend-web/src/styles/settings.css`, etc (page layer, but inconsistent + collision-prone class names)

**Global foundation already present**
- `apps/zerg/frontend-web/src/styles/layout.css` creates the global “living void” background and glassy `#app-container`.
- `apps/zerg/frontend-web/src/styles/css/nav.css` already borrows Jarvis-ish header/nav styling.

## CSS layering + scoping rules (how this repo works)
- Cascade order is declared in `apps/zerg/frontend-web/src/styles/layers.css` (`tokens` → `base` → `legacy` → `components` → `pages`).
- Global CSS is imported from `apps/zerg/frontend-web/src/main.tsx` in that order.
- Page CSS must be scoped under a page root class (e.g. `.dashboard-page { ... }`) to avoid collisions.
- Jarvis is already scoped under `.jarvis-container`; keep that boundary until primitives are extracted.

## Key findings (what makes Jarvis feel “premium”)
This is what we should systematize (not copy/paste).

### 1) Clear “surface language”
Jarvis uses a consistent ladder of surfaces:
- void background
- glass panel (blur + low-alpha fill)
- elevated surface (slightly higher alpha)
- borders (thin, low-alpha)
- inner highlights (subtle inset line)

### 2) Accent discipline (neon, but sparse)
Neon is used as:
- active indicator (underline / left bar)
- focus ring
- small accents on hover
Not as the default fill for everything.

### 3) Micro-interactions everywhere (but mostly cheap)
- fast hover transitions
- small translateY
- subtle glow
- staged entrance animations in focused contexts (sidebar items, messages)

### 4) Typography hierarchy
- display font for headings / product name
- monospace for meta (timestamps, system-ish UI)
- uppercase labels with spacing for “HUD” feel

### 5) “Delight layers” are optional
Jarvis has intensive elements (voice reactor rings, animated grids). Those should be opt-in for specific pages/components, not the default for information-dense screens.

## Critical issues blocking consistency (fix first)
These are concrete, repo-verifiable problems that prevent a cohesive rollout.

### A) Token drift: legacy CSS uses undefined spacing vars
Legacy CSS references `--spacing-xs/sm/md/lg/xl` (ex: `apps/zerg/frontend-web/src/styles/css/forms.css`, `apps/zerg/frontend-web/src/styles/css/dashboard.css`), but `apps/zerg/frontend-web/src/styles/tokens.css` defines `--space-*` (not `--spacing-*`).

This creates “random” spacing/visual weight depending on browser fallbacks (often: properties become unset).

### B) Collision-prone global class names
Examples found across page CSS:
- `.empty-state` appears in multiple unrelated files
- `.back-button` appears in multiple files
- `.section-header` appears in multiple files

This violates the repo’s own “scope styles under a container class” convention and makes it hard to roll out consistent primitives.

### C) Two styling paradigms coexist without a boundary
- Jarvis: route-local CSS, aggressively scoped under `.jarvis-container`
- Dashboard/pages: global CSS (legacy + page layers) with mixed scoping and hardcoded colors

We need a deliberate bridge: shared primitives/tokens, not shared page CSS.

## Proposed direction (decision required)
You said:
- you want the “cool” Jarvis feel everywhere
- but the heavy effects should be **simple/modular** and **toggleable via a single var**
- you want the UI to be **unified** (not “Jarvis vs dashboard”)

So this spec targets:
1) **Unified global UI system** (shared tokens + shared primitives, used by every page)
2) **Jarvis surface + motion language by default** (glass + subtle glow + good typography)
3) **Heavy FX are opt-in and one-switch** (background layers, infinite animations, extra glow)

Terminology (plain English):
- **“Route-local CSS”** = styles only loaded for `/chat` and scoped under `.jarvis-container`.
- **“Global primitives”** = shared building blocks (Button/Card/Table/etc) used everywhere.

The goal is: Jarvis’s “coolness” becomes the shared system, and `/chat` keeps only the truly chat-specific pieces.

## Heavy FX toggle (one switch)
Requirement: “turn it off in a single var”.

**Decision:** build-time Vite env var.

### Env var
- `VITE_UI_EFFECTS=on|off`
- Default: `on` (if undefined)
- Meaning:
  - `on` = enable ambient/background effects + infinite animations
  - `off` = disable heavy FX globally (no moving grids, nebula drift, reactor rings, etc.)

### Implementation shape (simple + modular)
1) In `apps/zerg/frontend-web/src/main.tsx`, set a single attribute:
   - `document.getElementById("react-root")?.setAttribute("data-ui-effects", value)`
2) Gate heavy effects in CSS via:
   - `#react-root[data-ui-effects="on"] { ... }`
   - `#react-root[data-ui-effects="off"] { ... }`
3) Respect reduced motion automatically:
   - `@media (prefers-reduced-motion: reduce)` should behave like `off` for animations.
     (Surfaces/typography still apply; only ambient loops stop.)

Examples of what gets gated behind the switch:
- animated grid layers (global `#react-root::before`, Jarvis grid floor, nebula drift)
- infinite glow pulses / reactor rings
- any non-essential “ambient” animations

Examples of what stays on even with FX off:
- base glass surfaces (blur + alpha fills)
- normal hover transitions
- typography + spacing
  (the app should still look “designed” with FX off)

### Concrete gating targets (existing code)
These already exist and should be moved behind the toggle:
- Global moving grid: `apps/zerg/frontend-web/src/styles/layout.css` (`#react-root::before`, keyframes `grid-move`)
- Jarvis background drift blobs: `apps/zerg/frontend-web/src/jarvis/styles/layout.css` (`.app-container::after`, `nebula-drift`)
- Jarvis grid floor: `apps/zerg/frontend-web/src/jarvis/styles/layout.css` (`.main-content::before`, `grid-drift`)
- Jarvis reactor rings: `apps/zerg/frontend-web/src/jarvis/styles/voice-button.css` (ring animations)

### Build-time reality check (Vite)
`VITE_*` vars are build-time. Changing `VITE_UI_EFFECTS` requires a rebuild of the frontend bundle (not just a container restart).
This is intentional: it keeps the runtime dead-simple (one compiled output).

## UI system spec (v1)
This is the “smallest useful design system” that lets us restyle the dashboard without a rewrite.

### Design tokens
Keep `apps/zerg/frontend-web/src/styles/tokens.css` as the single source of truth.

Add / standardize:
- **Legacy spacing aliases**: define `--spacing-xs/sm/md/lg/xl` to map to the existing `--space-*` scale.
- **Glass presets**: define a few named surface fills/borders/shadows used everywhere (e.g., `--surface-glass-1/2`, `--border-glass-1`), so we stop hardcoding RGBA.
- **Focus rings**: a single focus style (Jarvis cyan default; brand primary for links).

### Primitive components (shared building blocks)
Implement as lightweight React wrappers + one shared CSS file in `@layer components`.

Required primitives:
- `Button` (primary/secondary/ghost/danger + `IconButton`)
- `Card` (default/elevated + optional header slot)
- `Input`, `Textarea`, `Select`
- `Badge` (status pill variants: success/warn/error/neutral)
- `EmptyState` (illustration + title + body + action)
- `Table` (dense data table with sticky header + row hover + actions column pattern)

Rule: primitives must render with **predictable class names** (e.g., `ui-button ui-button--primary`) and must not rely on ad-hoc page CSS.

### Page layout patterns
Define 3 patterns we reuse across pages:
1) **Page shell**: max-width container + consistent padding + “glass section” background
2) **Section header**: title + description + right-aligned actions
3) **Card list**: grid/list layout for entity cards (agents/runners/etc)

### Motion + performance
Default:
- transitions: `--motion-duration-fast` / `--motion-easing-standard`
- avoid infinite animations on data-heavy pages
- respect `prefers-reduced-motion`

Optional (Jarvis-only or hero areas):
- animated grid floor
- nebula drift blobs
- reactor rings

## Rollout strategy (how we migrate without chaos)
The guiding idea: **stop styling pages directly** and start styling primitives. Then page CSS becomes mostly layout wiring.

### Phase 0 — Foundations (stop the bleeding)
1) Add missing legacy token aliases (`--spacing-*`, and any other legacy vars that are used but undefined).
2) Add the single FX env switch and gate existing heavy effects behind it.
3) Create `ui-*` primitives and migrate one or two low-risk components to prove the pattern.
4) Remove/avoid collision-prone global class names by scoping under a page root class.

### Phase 1 — Dashboard redesign (first big win)
Convert `DashboardPage` to:
- use `PageShell` + `SectionHeader`
- use `Table` + `Badge` + `IconButton`
- adopt Jarvis surface language (glass card, borders, hover)

Goal: dashboard looks like “Jarvis’s sibling”, not “a different app”.

### Phase 2 — Expand to other pages
Migrate in priority order:
1) `/runners` + runner detail (lots of one-off styling today)
2) `/settings` (forms: the primitives will pay off immediately)
3) `/admin` (tables + cards)
4) `/canvas` (only the shell chrome; keep canvas-specific visuals separate)

### Phase 3 — Delete legacy styling paths
After each page is migrated, remove its reliance on the corresponding legacy CSS chunk (or leave it but ensure it no longer matches any live selectors).

## Acceptance criteria (done means done)
**Global:**
- With `VITE_UI_EFFECTS=off`, there are no ambient/infinite animations (no moving grids/nebula drift/reactor rings), but the UI still looks cohesive and glassy.
- With `VITE_UI_EFFECTS=on`, ambient effects appear where intended (and remain scoped / not leaking).
- No pages rely on undefined CSS variables (no `--spacing-*` drift).

**Dashboard:**
- `/dashboard` matches the Jarvis surface language: glass cards, consistent borders, hover/focus style, consistent typography.
- Page CSS selectors are scoped under a page root class (no generic `.empty-state`, `.back-button`, etc. at top-level).

## Progress Summary
All phases of the Jarvis Look-and-Feel rollout have been implemented. The app now uses a unified UI system based on shared primitives and design tokens. Heavy effects are gated behind the `VITE_UI_EFFECTS` environment variable.

## Task list (Completed)
This is intentionally concrete (files + outcomes).

### P0 — Foundations
- [x] Add legacy spacing aliases in `apps/zerg/frontend-web/src/styles/tokens.css` (`--spacing-xs/sm/md/lg/xl`).
- [x] Audit for other undefined legacy vars (`--text-secondary`, `--dark-lighter`, etc.) and either alias or remove usage.
- [x] Add `VITE_UI_EFFECTS` handling in `apps/zerg/frontend-web/src/main.tsx` to set `data-ui-effects` on `#react-root`.
- [x] Gate heavy FX behind `#react-root[data-ui-effects="on"]` in:
  - [x] `apps/zerg/frontend-web/src/styles/layout.css` (global moving grid)
  - [x] `apps/zerg/frontend-web/src/jarvis/styles/layout.css` (grid floor + nebula drift)
  - [x] `apps/zerg/frontend-web/src/jarvis/styles/voice-button.css` (reactor animations)
- [x] Create `apps/zerg/frontend-web/src/styles/ui.css` in `@layer components` for shared primitives.
- [x] Add `apps/zerg/frontend-web/src/components/ui/` primitives: `Button`, `IconButton`, `Card`, `Badge`, `Input`, `EmptyState`, `Table`.
- [x] Replace ad-hoc `.empty-state` usage with a single `EmptyState` component and scoped page wrappers.

### P1 — Dashboard redesign
- [x] Add a `dashboard-page` root class and move any remaining dashboard-specific CSS into a `@layer pages` file scoped under it.
- [x] Convert action buttons to `IconButton` variants.
- [x] Convert status pills to `Badge` variants.
- [x] Convert “Create Agent” to `Button --primary` (Jarvis-style hover + subtle glow).
- [x] Redesign agents table: sticky header surface, row hover, expanded row styling consistent with glass cards.

### P2 — Forms + settings
- [x] Replace legacy form styling with `Input`/`Select` primitives (consistent focus ring + spacing).
- [x] Rework Settings sections to use `SectionHeader` + `Card`.

### P2 — Runners + cards
- [x] Replace hardcoded colors in `apps/zerg/frontend-web/src/styles/runners.css` with tokens + primitives.
- [x] Normalize runner status badges to shared `Badge`.

### P3 — Cleanup + guardrails
- [x] Add a small “UI demo” route or hidden page (optional) to visually inspect primitives in one place.
- [x] Add Playwright screenshots for `/chat` and `/dashboard` (optional but high leverage) so styling regressions are obvious.
- [x] Remove dead/unreferenced selectors after migration (only when safe).

## Assumptions (locked in for execution)
- We progressively migrate shared Jarvis styling into global primitives; `/chat` keeps only chat-specific visuals (message bubbles, voice controls).
