# CI Provisioning + Smoke Worklog (High + Low Level)

Last updated: 2026-02-05

## High-level summary

Goal: make CI signal trustworthy and aligned with the "provision per push" vision.

What is now true:
- Provisioning E2E (control plane + instance) runs on cube ARC and passes.
- Smoke-after-deploy now validates same-origin instance health and no longer hits marketing.
- Frontend unit flake fixed; Playwright deps installed on cube; smoke gate stable.

What is still in flight:
- Push-time E2E still needs final stabilization (port cache / stale backend cleanup).
- "Smoke on push" should move into provisioning E2E; scheduled smoke should be manual or point to a dedicated smoke instance.

## Low-level change log (repo)

### CI / workflow changes
- `.github/workflows/contract-first-ci.yml`
  - Added per-run `E2E_DB_DIR` to avoid cross-run contamination.
  - Upload E2E artifacts on failure.
  - Playwright install now uses `--with-deps` for cube runner.
- `.github/workflows/smoke-after-deploy.yml`
  - Defaults to same-origin instance (`https://david.longhouse.ai`).
  - Uses `/api/health` and passes envs to smoke script.
  - Added schedule gate (`SMOKE_AFTER_DEPLOY_ENABLED` var) to stop spam when needed.

### Smoke script / prod e2e
- `scripts/smoke-prod.sh`
  - Default API/Frontend to same-origin.
  - Accepts `/api` in config.js for same-origin deployments.
  - Skips CORS checks when same-origin.
  - Auto-skips LLM tests when `/api/system/capabilities` reports `llm_available=false`.
- `scripts/run-prod-e2e.sh`
  - Defaults to same-origin instance.

### Frontend unit test flake
- `apps/zerg/frontend-web/src/components/SessionPickerModal.tsx`
  - Clears focus timer on unmount to avoid `window is not defined` after teardown.

### Makefile
- Randomized E2E ports by default (no more fixed 8001/8002 unless explicitly set).

### TODO updates
- Added `[QA/Test] CI Stability — E2E + Smoke` workstream to track remaining steps.

## Ops changes (prod)

NOTE: these were *manual* emergency fixes to get CI signal back; see the clarification below.

- Re-created `longhouse-david` container manually on zerg with:
  - `PUBLIC_SITE_URL=https://david.longhouse.ai`
  - `SINGLE_TENANT=0`
  - `SMOKE_TEST_SECRET` set (value stored in GH secrets)
  - latest runtime image pulled
- `/api/health` now returns JSON on `david.longhouse.ai`.
- `SMOKE_TEST_SECRET` set in GitHub Actions secrets (repo-level).

### Why “manual”
The instance was re-created via `docker run` on the zerg host to stabilize health + smoke quickly. This bypassed the intended provisioning flow because the control plane provisioning system isn’t yet wired to own prod instances. This is not the long-term plan.

Follow-up action: codify this via control plane provisioning (or a reproducible script) and delete any ad‑hoc manual steps.

## Current CI health

- Smoke-after-deploy: ✅ green after same-origin + LLM gating fixes.
- Provisioning E2E: ✅ green on cube ARC.
- Push/PR CI: ❌ E2E still failing (root cause was missing Playwright deps; fixed, but still verifying). Likely remaining issue: Playwright port cache + stale backend on shared runner.

## Known blockers / next actions

1) **E2E on cube still flaky**
   - Root cause now appears to be browser deps on cube (fixed with `--with-deps`) plus possible stale backend reuse.
   - Planned fix: disable Playwright port cache in CI or hard‑kill stray backend before E2E.

2) **Move “smoke on push” into provisioning E2E**
   - Desired behavior: provision ephemeral instance, run smoke against it, then delete.
   - Keep scheduled smoke (if desired) as a separate workflow and point to a dedicated smoke instance.

3) **Replace manual instance provisioning**
   - Convert the manual `docker run` to control-plane or Coolify‑managed flow.
   - Ensure config matches same‑origin architecture.

## Artifacts & evidence

- E2E artifacts are now uploaded on failure in CI (Playwright report + test-results).
- Smoke script and runtime health now validated against same‑origin instance.
