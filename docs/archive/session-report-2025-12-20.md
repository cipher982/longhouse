# ⚠️ ARCHIVED / HISTORICAL REFERENCE ONLY

> **Note:** Paths and implementation details in this document may be outdated.
> For current information, refer to [AGENTS.md](../../AGENTS.md) or the root `docs/README.md`.

---

# Session Report: Swarmlet SPA Merge Cleanup & Stabilization

**Date:** 2025-12-20
**Focus:** Post-merge review cleanup, bug fixes, and test stabilization.

## Summary
Successfully completed high-priority (P0/P1) tasks from the post-merge review. The Zerg + Oikos unification is now code-complete, with styling scoped, dead code removed, and configuration updated. E2E test performance remains an open investigation item.

## Key Accomplishments

### 1. Stylesheet Isolation (P0)
- **Problem:** Oikos global styles (e.g., `body`, `*` selectors) were leaking into the Zerg SPA, affecting other pages.
- **Fix:** Refactored all Oikos CSS (`apps/zerg/frontend-web/src/oikos/styles/*.css`) to be scoped under a root class `.oikos-container`.
- **Refinement:** Updated `theme-glass.css` to scope CSS variables under `.oikos-container` instead of `:root`.
- **Outcome:** Oikos styling is completely contained within its route.

### 2. UI Integration & Component Logic (P1)
- **Problem:** `/chat` rendered two headers (Zerg global + Oikos internal).
- **Fix:** Added `embedded` prop to Oikos `App` component. Updated `OikosChatPage` to pass `embedded={true}`.
- **Outcome:** Oikos internal header is hidden when mounted within the SPA.

### 3. Dead Code Removal (P1)
- **Problem:** `apps/zerg/frontend-web/src/oikos/` (legacy standalone frontend) was redundant and causing confusion.
- **Action:** Deleted the entire directory.
- **Outcome:** Single source of truth for frontend code.

### 4. Configuration & Hygiene
- **Docker:** Removed `oikos-web` service from `docker/docker-compose.prod.yml` and `scripts/dev-docker.sh`.
- **Makefile:** Updated targets to remove references to the deleted legacy app (`test-oikos`, `oikos`, etc.).
- **Tests:** Fixed `scripts/verify-single-react.mjs` to stop checking the deleted workspace.

### 5. E2E Test Configuration (Ongoing)
- **Action:** Configured `playwright.config.js` to use all available CPU cores (16) locally.
- **Action:** Updated `spawn-test-backend.js` to:
    - Scale `uvicorn` workers based on CPU count.
    - Suppress backend logs by default (unless `VERBOSE_BACKEND` is set) to reduce noise.
- **Status:** Configuration is correct, but execution performance (parallelism) needs further investigation.

## Artifacts Updated
- `apps/zerg/frontend-web/src/oikos/styles/**`
- `apps/zerg/frontend-web/src/pages/OikosChatPage.tsx`
- `apps/zerg/frontend-web/src/oikos/app/App.tsx`
- `apps/zerg/e2e/playwright.config.js`
- `apps/zerg/e2e/spawn-test-backend.js`
- `Makefile`
- `docker/docker-compose.prod.yml`
- `scripts/dev-docker.sh`
- `scripts/verify-single-react.mjs`

## Next Steps
1.  **Investigate E2E Parallelism:** Why does the suite feel serial/slow despite correct config? (Potential: Database locking, resource contention, or Playwright scheduler behavior).
2.  **Monitor Production:** Verify the deployed SPA behaves as expected with the new CSS scoping.
3.  **Future Cleanup:** Rename Docker profiles (`full` -> `dev`) and consolidate E2E harnesses.
