# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---

# Swarmlet SPA Merge — Post‑Merge Review + Cleanup Spec

**Date:** 2025-12-20
**Scope:** Zerg + Jarvis unified frontend (single SPA)

## Executive Summary
The core merge is **COMPLETE and VERIFIED**.
- `/chat` is served by the Zerg React app (SPA).
- `apps/zerg/frontend-web/src/jarvis/` has been **deleted**.
- CSS leakage has been fixed by scoping Jarvis styles under `.jarvis-container`.
- Duplicate header issue is resolved via `embedded` prop.
- `docker/docker-compose.prod.yml` and scripts have been updated to remove `jarvis-web`.

---

## Verified Current State (repo reality)

### Frontend
- `/chat` is served by the Zerg React app and mounts Jarvis from `apps/zerg/frontend-web/src/jarvis/` via `apps/zerg/frontend-web/src/pages/JarvisChatPage.tsx`.
- `JarvisChatPage` wraps the app in `.jarvis-container`.
- `App.tsx` respects `embedded={true}` to hide the internal header.
- **Verification:** `bun run test` in `apps/zerg/frontend-web` PASSES.

### Backend
- Backend tests PASS (`apps/zerg/backend`: `./run_backend_tests.sh` - ~2m40s).

### Docker / nginx
- `docker/docker-compose.prod.yml` no longer defines `jarvis-web`.
- `scripts/dev-docker.sh` no longer waits for `jarvis-web` logs.

### Repo hygiene
- `apps/zerg/frontend-web/src/jarvis/` is **DELETED**.
- `Makefile` targets updated to remove dead references.

---

## Completed Follow-up List

### P0 — Must Fix (ship-stopper risk)

1) **[DONE] Jarvis CSS leakage across the SPA**
- Refactored all CSS in `apps/zerg/frontend-web/src/jarvis/styles/` to scope under `.jarvis-container`.
- Updated `theme-glass.css` to scope variables under `.jarvis-container`.

2) **[DONE] Ensure `apps/zerg/frontend-web/src/jarvis/` is committed**
- Files are tracked in git.

### P1 — High Impact UX Simplifications

3) **[DONE] Duplicate header on `/chat`**
- `App.tsx` updated to accept `embedded` prop.
- `JarvisChatPage` passes `embedded={true}`.

4) **[DONE] Remove dead standalone Jarvis frontend**
- `apps/zerg/frontend-web/src/jarvis/` deleted.

5) **[DONE] Fix “prod nginx config drift”**
- `docker/docker-compose.prod.yml` updated to remove `jarvis-web`.

### P2 — Medium Impact Simplifications (architectural hygiene)

6) **Docker profile naming clarity** (Pending/Next)
- Renaming profiles (`full` -> `dev`, `zerg` -> `direct`) is still a valid future task.

7) **Model config source-of-truth** (Pending)
- Drift risk still exists for model config.

8) **Jarvis BFF endpoints naming** (Pending)
- `/api/jarvis/*` remains.

9) **E2E test environment consolidation** (Pending)
- `apps/jarvis` still acts as the E2E harness. This is acceptable for now.

### Known Issues

10) **E2E Test Performance**
- Tests run serially despite configuration for parallelism (16 workers).
- Attempts to fix via `playwright.config.js` worker count and `uvicorn --workers` were made but not fully successful in reducing runtime below ~8-9 mins.
- Root cause investigation required (suspect: `spawn-test-backend.js` process management or Playwright worker distribution).

---

## Next Steps
- **Investigate E2E Parallelism:** Deep dive into why Playwright/Backend is serializing tests.
- **Monitor E2E Stability:** Ensure the merged SPA doesn't regress on tests.
- **Address P2 items:** Profile renaming and model config drift.
