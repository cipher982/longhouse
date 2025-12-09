# React Frontend Migration Playbook

This guide provides the end-to-end context, workflows, and task roadmap for replacing the Rust/WASM frontend with the new React stack that lives in `frontend-web/`.

---

## 1. Project Context

### Goals

- Achieve **feature parity** between the legacy Rust UI and the React prototype before the final cutover.
- Keep **back-end contracts and automated tests** green during the strangler phase.
- Offer a **single-command development workflow** so engineers can switch between UIs effortlessly.

### Architecture Snapshot

| Layer           | Tech                                                              | Notes                                                                                                                    |
| --------------- | ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
| Backend         | FastAPI (`backend/`), PostgreSQL                                  | REST endpoints under `/api`, WebSocket topic manager under `/api/ws`. Contracts generated from AsyncAPI/OpenAPI specs.   |
| Legacy Frontend | Rust + wasm-bindgen (`frontend/`)                                 | Compiled by `build-debug.sh`, renders into `frontend/www/index.html`, styles live in `frontend/www/css/` and `chat.css`. |
| React Pilot     | Vite + React 18 + React Router + TanStack Query (`frontend-web/`) | Builds to static assets copied into `frontend/www/react/` so the same dev server can host both bundles.                  |
| Testing         | Playwright (`e2e/`), pytest, Vitest (planned)                     | Fixtures can flip between Rust and React by seeding localStorage flags and worker IDs.                                   |

### Local Dev Workflow

1. Install deps once at repo root: `npm install`
2. Run everything with **`make start`**
   - Starts FastAPI backend (default `http://localhost:8001`).
   - Runs the Rust build + static server on `http://localhost:8002`.
   - Builds the React bundle and copies it to `frontend/www/react/`.
   - Launches the browser to `http://localhost:8002/ui-switch.html`.
3. Use the **UI switch page** to pick either UI:
   - “Open Rust / WASM UI” → `http://localhost:8002/`
   - “Open React Prototype” → `http://localhost:8002/react/index.html`

### Contracts & Flags

- `frontend/www/config.js` sets `window.API_BASE_URL` and `window.WS_BASE_URL` so the React bundle hits the FastAPI backend even though it’s served from the static server port.
- Playwright still seeds `zerg_use_react_*` localStorage keys when needed, but manual toggling is no longer required.
- WebSockets include the `worker` query parameter for Playwright isolation; React components read `__TEST_WORKER_ID__` to mirror the legacy behaviour.

### Codebase Quirks

- Large shared CSS bundle; React components currently import `src/styles/legacy.css` to reuse selectors. Gradually retire this as components get redesigned.
- Build script (`frontend/build-debug.sh`) now owns both the Rust build and the React Vite build. Be mindful of build times.
- Contracts are generated—modifying payloads requires regen + updates across backend, Rust, and React.

---

## 2. Migration Backlog

Below is the high-level plan for finishing the React transition. Each bullet can be broken into smaller tickets.

### 2.1 Feature Parity

- **Dashboard**: scope toggle (My vs All agents), search/filter, owner column, stats, inline status pills, bulk actions (reset, rerun).
- **Agent detail & Threads**: rename/delete thread actions, modal UX, history table, tool outputs, streaming indicators.
- **Chat**: message retries, tool disclosure components, assistant avatars, keyboard shortcuts, attachments (if any in Rust UI).
- **Canvas**: workflow CRUD, autosave layout, node configuration modals, execution runner UI, log drawer, template gallery, keyboard shortcuts.
- **Admin/Ops Views**: reset DB panel, ops dashboards, system metrics, experiment toggles.
- Maintain parity for Toasts, modals, global nav, status indicators.

### 2.2 Shared API & State Layer

- Extract REST helpers and TypeScript types into a shared module (could be generated via OpenAPI/AsyncAPI).
- Standardise auth token handling and query invalidation logic.
- Consider a cross-frontend data layer (e.g., package like `@zerg/contracts`) shared by Rust `wasm_bindgen` and React.

### 2.3 Styling & Design System

- Catalogue legacy CSS tokens (colours, spacing, radius) and codify them (CSS vars, Tailwind config, etc.).
- Replace ad-hoc classnames with component-scoped styles as React components mature.
- Ensure accessibility (focus outlines, contrast) matches the legacy experience or improves it.

### 2.4 Testing & Quality Gates

- Add component tests with Vitest/RTL for the React bundle (dashboard table, chat compose, canvas node editor).
- Expand Playwright coverage for `/react/...` routes; add a CI job (`npm run build`, `npm run lint`, targeted Playwright smoke suite).
- Keep `make test-e2e` green by aligning fixtures for both UI paths.

### 2.5 Feature Flag Strategy

- Move from localStorage toggles to server-configured flags (e.g., environment-based or stored in DB).
- Allow per-environment rollout (dev → staging → prod) and user targeting if needed.
- Plan the final cutover: once React is feature-complete, redirect `/` to the React bundle and retire the Rust assets.

### 2.6 Deployment Plan

- Decide whether prod builds re-use the current static server or ship to a CDN (affects caching/CSP).
- Update deployment scripts (Coolify) to run the React build step and publish assets.
- Review CSP headers (`frontend/www/index.html`) to ensure React assets load without loosening security.

### 2.7 Developer Experience

- Document the new workflow in `README.md` (currently only in this doc and `CLAUDE.md`).
- Provide helper scripts for watch mode (`npm run dev`) without breaking `make start` simplicity.
- Monitor build duration; consider toggles for skipping the React build during quick Rust iterations.

---

## 3. Onboarding Checklist for New Contributors

1. **Read this document**, `README.md`, and `CLAUDE.md` for environment context.
2. **Install dependencies** (`npm install` at repo root, `uv`/Python environment for backend).
3. **Run `make start`** and exercise both UIs via `http://localhost:8002/ui-switch.html`.
4. **Explore the React code** (`frontend-web/src/`), noting service helpers, pages, and shared components.
5. **Trace API flows**: look at `frontend-web/src/services/api.ts` and corresponding FastAPI routers under `backend/zerg/routers/`.
6. **Review Playwright fixtures** (`e2e/tests/fixtures.ts`) to understand how tests interact with flags and worker IDs.
7. **Pick a parity task** from Section 2.1, implement it in React, and ensure existing tests (and any new ones) pass.
8. **Update docs/tests** alongside feature work to keep the roadmap current.

---

## 4. Dashboard Parity Contract

Use this checklist whenever the React dashboard changes. The goal is to keep the React markup, styles, and baseline visuals aligned with the Rust/WASM implementation until the redesign phase officially begins.

- **DOM structure:** `#dashboard-container > #dashboard > .dashboard-header + #agents-table` must match the legacy hierarchy. Keep legacy IDs/classes (`dashboard-header`, `button-container`, `agents-table`, `status-indicator`, etc.) so the shared CSS bundle continues to apply.
- **Feature gating:** Hide scope toggles, stats decks, bulk selection panels, and bespoke React adornments unless the same feature ships in Rust. When we re-introduce them, wrap behind a feature flag and update this section.
- **Styling:** Do not reintroduce `frontend-web/src/styles/dashboard.css`. The React bundle should pull styling exclusively from `src/styles/legacy.css`, adding component-specific styles only when scoped and unavoidable.
- **Markup parity test:** Run `pnpm --filter frontend-web test`. The Vitest suite snapshots `#dashboard-container` against `src/pages/__tests__/__fixtures__/legacy-dashboard.html`. Update the fixture only when the Rust markup intentionally changes.
- **Visual diff guard:** Run `pnpm --filter e2e test -- --grep "React dashboard matches legacy visual baseline"`. The Playwright spec compares the React screenshot against `e2e/visual-baseline/dashboard-legacy.png`. Refresh the baseline by loading the Rust dashboard, taking a screenshot of `#dashboard-container`, and overwriting that PNG (commit the diff alongside code changes).
- **Custom events:** React action buttons emit `dashboard:event` custom events (`run`, `edit`, `debug`, `delete`, `run-actions`) so the host shell can wire them to legacy behaviours. Maintain those event names when adding new handlers.

If any checklist item needs to change, update this contract, the Vitest fixture, and the Playwright baseline in the same pull request so reviewers can reason about the delta.

## 5. Historical Notes (Legacy Instructions)

The original strangler instructions (React dev server on port 3000, manual localStorage flags) are retained below for reference. They are no longer required for day-to-day work but remain useful for debugging Playwright or legacy workflows.

### Local Development (Legacy Approach)

1. Install dependencies via the existing root workspace:

   ```bash
   npm install
   ```

2. Start the React dev server:

   ```bash
   cd frontend-web
   npm run dev
   ```

   The Vite server runs on `http://localhost:3000` and proxies API calls to the FastAPI backend on `:8001`.

3. Enable the strangler flag in the legacy UI. In the browser console run:

   ```javascript
   localStorage.setItem("zerg_use_react_dashboard", "1");
   localStorage.setItem(
     "zerg_react_dashboard_url",
     "http://localhost:3000/dashboard",
   );
   ```

4. Disable the flag by clearing the keys:

   ```javascript
   localStorage.removeItem("zerg_use_react_dashboard");
   localStorage.removeItem("zerg_react_dashboard_url");
   ```

### React Chat Pilot (Legacy Strangler)

```javascript
localStorage.setItem("zerg_use_react_chat", "1");
localStorage.setItem("zerg_react_chat_base", "http://localhost:3000/chat");
```

When the legacy SPA navigates to the chat view it will redirect to `/chat/<agentId>/<threadId?>` at the provided base URL. Remove the keys to return to the Rust implementation.

---

This playbook should give any new developer enough context to understand the architecture, run the project quickly, and meaningfully contribute to the remaining migration tasks.
