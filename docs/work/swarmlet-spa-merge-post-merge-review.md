# Swarmlet SPA Merge — Post‑Merge Review + Cleanup Spec

**Date:** 2025-12-20
**Scope:** Zerg + Jarvis unified frontend (single SPA)

## Executive Summary
The core merge is real and working: `/chat` is now a route in the Zerg SPA and renders the Jarvis chat UI from `apps/zerg/frontend-web/src/jarvis/`. Docker dev/prod compose configs no longer run a separate `jarvis-web` service, and the build output contains Jarvis chunks.

There are a few **high-impact follow-ups** (CSS leakage + duplicate header + repo hygiene), plus some medium/longer-term simplifications (Docker profile naming, config/model source-of-truth, BFF naming, E2E consolidation).

---

## Verified Current State (repo reality)

### Frontend
- `/chat` is served by the Zerg React app and mounts Jarvis from `apps/zerg/frontend-web/src/jarvis/` via `apps/zerg/frontend-web/src/pages/JarvisChatPage.tsx`.
- Zerg frontend production build includes Jarvis artifacts (example):
  - `dist/assets/JarvisChatPage-*.css` (~46kB)
  - `dist/assets/JarvisChatPage-*.js` (~350kB)
- Zerg frontend unit tests pass (`apps/zerg/frontend-web`: `bun run test`).

### Backend
- Backend tests pass (`apps/zerg/backend`: `./run_backend_tests.sh`).

### Docker / nginx
- `docker/docker-compose.dev.yml` no longer defines `jarvis-web` services; reverse-proxy depends only on `zerg-frontend` + `zerg-backend`.
- `docker/nginx/docker-compose.unified.conf` and `docker/nginx/docker-compose.prod.conf` route `/chat` to the Zerg frontend (no Jarvis upstream).
- **Important:** `docker/nginx/nginx.prod.conf` must not reference `jarvis-web` anymore (this should be true after cleanup).

### Repo hygiene (must be true before calling this “done”)
- `apps/zerg/frontend-web/src/jarvis/` must be **tracked and committed** (it being untracked locally means the merge will “work on one machine” only).

---

## Final Follow-up List (prioritized)

### P0 — Must Fix (ship-stopper risk)

1) **Jarvis CSS leakage across the SPA**
- Current Jarvis CSS includes global selectors (`*`, `html`, `body`, `body::before`, etc.).
- In a SPA, once `/chat` is visited, those global rules remain in the document and can silently affect `/dashboard` and other routes.
- This also conflicts with the repo’s “scope CSS under a container” rule.

**Spec / acceptance**
- Visiting `/chat` then navigating to `/dashboard` must not change global `body/html` styles, scroll model, overlays, or typography.
- Jarvis styling should be applied only within a container such as `.jarvis-container` (or mount/unmount a dedicated stylesheet cleanly).

**Implementation options**
- **Option A (recommended):** refactor Jarvis CSS to scope under a container (`.jarvis-container { ... }` + `:where(.jarvis-container *) { ... }`), remove `html/body` rules, and use a Jarvis-local scroll container.
- Option B: keep global CSS but mount/unmount a `<style>` tag on route entry/exit (works, but still violates “no globals” and can cause transient flash or layout shifts).

2) **Ensure `src/jarvis/` is committed**
- If `apps/zerg/frontend-web/src/jarvis/` is not committed, the “merge” is not reproducible.

**Spec / acceptance**
- Clean checkout of `main` should build `/chat` without requiring any local file copying.

### P1 — High Impact UX Simplifications

3) **Duplicate header on `/chat`**
- `/chat` currently shows:
  - Zerg global header (tabs)
  - Jarvis internal header (duplicate nav/controls)

**Spec / acceptance**
- `/chat` should have **one** top-level navigation header (Zerg’s).
- Jarvis may keep *in-chat* controls, but not a second “app nav” bar.

**Implementation options**
- Add an `embedded` flag to Jarvis app (`<App embedded />`) to hide the Jarvis header.
- Or move Jarvis header responsibilities into Zerg shell and delete the Jarvis header component.

4) **Remove dead standalone Jarvis frontend (or archive it clearly)**
- `apps/jarvis/apps/web/` is now redundant as a served frontend.

**Decision**
- **Delete** if you want zero drift risk.
- **Archive** if you want a reference implementation (but enforce “not served” and avoid double-maintenance).

**Spec / acceptance**
- Docker dev/prod does not build or run `apps/jarvis/apps/web`.
- `make test` remains green (update targets if directory removed).

5) **Fix “prod nginx config drift”**
- There are multiple nginx configs in the repo. Some are used in Docker, some in Coolify/prod. They must all agree that `/chat` is served by the Zerg frontend.

**Spec / acceptance**
- No nginx config under `docker/nginx/` routes `/chat` to `jarvis-web`.

### P2 — Medium Impact Simplifications (architectural hygiene)

6) **Docker profile naming clarity**
- `full` used to mean “Zerg + Jarvis + nginx”, now it’s basically “nginx entrypoint dev”.

**Options**
- Rename `full` → `dev` (or `proxy`) to reflect “nginx entrypoint”.
- Rename `zerg` → `direct` (or `zerg-direct`) to reflect “direct ports”.
- Keep `prod` as-is.

**Spec / acceptance**
- Make targets and docs use the new names; no ambiguity about what runs.

7) **Model config source-of-truth**
- Jarvis model config is currently inlined in `apps/zerg/frontend-web/src/jarvis/core/model-config.ts` to avoid Docker/workspace resolution issues.
- Drift risk: `config/models.json` changes won’t auto-update Jarvis.

**Options**
- Move the needed config into `apps/zerg/frontend-web/src/jarvis/core/` as JSON and import it normally.
- Or keep the workspace package but fix Docker build context to include it.
- Or keep current inline approach + add a check/script to prevent drift.

8) **Jarvis BFF endpoints naming (`/api/jarvis/*`)**
- Now that it’s one app, naming can be simplified later, but it’s not required for the merge.

**Decision (recommended for now)**
- Keep `/api/jarvis/*` until there’s a deliberate API surface redesign.

9) **E2E test environment consolidation**
- Today there are separate E2E environments under `apps/jarvis/` and `apps/zerg/e2e/`.

**Options**
- Keep both for now (least risk).
- Or converge on a single Playwright harness and remove the redundant one.

### P3 — Low Impact / Later

10) **Theme + token unification**
- Jarvis still has a distinct theme layer; long-term it can be migrated onto the shared token/cascade system.

11) **Auth code overlap**
- Zerg and Jarvis both contain auth-related logic; evaluate overlap once the UI is stable and CSS is scoped.

---

## Decisions to Lock (so follow-ups don’t churn)
1) Remove vs archive `apps/jarvis/apps/web/`:
   - Default recommendation: **delete** (avoid drift).
2) CSS strategy:
   - Default recommendation: **Option A (scope Jarvis CSS under `.jarvis-container`)**.
3) Keep `/api/jarvis/*` for now:
   - Default recommendation: **yes** (defer API redesign).
4) Docker profile rename:
   - Default recommendation: **yes**, but do it as a focused rename PR so it doesn’t mix with UI work.
