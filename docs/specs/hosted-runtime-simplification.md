# Hosted Runtime Simplification

**Status:** Draft
**Author:** David + Codex
**Date:** 2026-03-05

## Executive Summary

Longhouse has one runtime product but four operational personas:

1. OSS local install (`longhouse serve`, `longhouse connect`)
2. Hosted paid instances (future customers)
3. Hosted dev instance (`david010.longhouse.ai`)
4. CI automation

The runtime is mostly the same in all four cases. The control surfaces are not. Deploy, auth, smoke, and live QA have drifted into separate paths with different assumptions, which is why small hosted changes keep breaking automation.

This spec simplifies the hosted side down to:

- one canonical hosted target identifier: `subdomain`
- one canonical hosted URL source: control plane response
- one canonical hosted auth path: `POST /api/instances/{id}/login-token` -> `POST /api/auth/accept-token`
- one canonical hosted smoke entrypoint shared by humans, CI, and post-deploy verification
- two CI lanes: fast runtime smoke against `david010`, slower provisioning E2E against ephemeral instances

OSS local remains separate and intentionally simple.

## First-Principles Rule

For a solo founder, the right shape is not "support every environment with shims." It is:

- **One hosted runtime path**
- **One hosted auth exchange**
- **One hosted smoke harness**
- **One source of truth for instance URL**
- **One slower provisioning E2E test** kept separate from fast runtime verification

Anything else is derived from those primitives.

## Problem Statement

The current hosted process leaks implementation history into operations.

### Drift Points in the Repo

- Hosted smoke still defaults to `https://david.longhouse.ai` in `.github/workflows/smoke-after-deploy.yml` and `scripts/smoke-prod.sh`.
- Deploy-and-verify still assumes a compose-managed hosted instance in `.github/workflows/deploy-and-verify.yml`.
- Hosted authenticated smoke still uses `SMOKE_TEST_SECRET` and `/api/auth/service-login` in `scripts/smoke-prod.sh`.
- Live QA still uses password scraping from the running container in `scripts/qa-live.sh`.
- Production E2E still assumes `SMOKE_TEST_SECRET` in `scripts/run-prod-e2e.sh`.
- The product's actual hosted auth model is already token exchange via control plane login token plus instance `accept-token`, but hosted automation does not use it.

### Why This Keeps Breaking

The control plane is the real hosted source of truth. Reprovision replaces containers from control-plane config. Any secret or hostname assumption that lives outside that model will drift.

That is exactly what happened with:

- stale alias hostname assumptions (`david.longhouse.ai`)
- manually injected per-container `SMOKE_TEST_SECRET`
- workflows that encode a different hosted lifecycle than the product actually uses

## Design Goals

1. **Keep OSS local untouched** — no control-plane dependency for install-script or local-serve users.
2. **Make hosted paid, hosted dev, and hosted CI use the same primitives**.
3. **Use short-lived auth for hosted automation** — no static per-instance smoke secret.
4. **Resolve hosted targets from control-plane data, not workflow literals**.
5. **Separate fast runtime smoke from slower provisioning E2E**.
6. **Remove fallback trees** — pre-launch, no need to preserve broken legacy paths.
7. **Stay shell-friendly and small** — a little shared helper code is better than a new subsystem.

## Non-Goals

- New orchestrator (no Kubernetes/Nomad migration)
- Full environment/label registry
- Backward compatibility for `david.longhouse.ai` unless explicitly reintroduced as a first-class alias
- Replacing the control plane with Coolify APIs
- GitHub OIDC migration in the same sprint
- Changes to OSS onboarding commands (`install.sh`, `longhouse serve`, `longhouse connect`)

## Current State vs Target State

### Current State

- Hosted target identity is implicit and often hardcoded.
- Hosted URL is duplicated across workflows and scripts.
- Hosted smoke auth is a separate test-only secret flow.
- Post-deploy verification, live QA, runtime smoke, and prod E2E all authenticate differently.
- CI runtime checks and provisioning checks are partially conflated.

### Target State

- Hosted target identity is `subdomain`.
- Hosted URL is returned by the control plane or derived once in a shared helper from control-plane data.
- Hosted smoke auth always uses `login-token -> accept-token`.
- Live QA and prod E2E use the same hosted auth helper.
- Fast runtime smoke hits persistent dev (`david010`) only.
- Slower provisioning E2E proves the full control-plane provisioning path separately.

## Canonical Environment Model

### 1. OSS Local

This path stays separate.

- Install via `install.sh` or `pip install`
- Run `longhouse serve`
- Optional `longhouse connect`
- No control plane involved

This is not a special case of hosted. It is a different product mode.

### 2. Hosted Persistent Instance

This covers both future paid customers and the current dev instance.

- Canonical identifier: `subdomain`
- Canonical URL: `https://{subdomain}.{root_domain}`
- Lifecycle owner: control plane
- Auth for browser automation: login token exchanged for instance cookie

`david010` is simply the current persistent hosted target. It should not need unique scripts.

### 3. Hosted Ephemeral Instance

This is CI-only and uses the same provisioning path.

- Created by control plane admin API
- Gets a real hosted URL
- Uses the same login-token auth exchange
- Destroyed after the test run

This remains the right place to verify provisioning end-to-end.

## Core Decisions

### Decision 1: `subdomain` is the only hosted selector in this sprint

Do not add `purpose`, labels, aliases, or environment classes to the database yet.

Why:

- pre-launch, single operator
- `david010` is the only persistent hosted test target
- `subdomain` already exists everywhere
- adding a selector registry now adds more code than value

If more persistent hosted instances appear later, we can add `purpose` in a follow-up. Not in this sprint.

### Decision 2: hosted automation uses product auth, not test auth

Hosted smoke, live QA, and prod E2E all authenticate this way:

1. Resolve instance by `subdomain` via control plane admin API
2. `POST /api/instances/{id}/login-token`
3. `POST {instance_url}/api/auth/accept-token`
4. Reuse returned cookie jar for authenticated checks

`/api/auth/service-login` is no longer part of hosted automation.

### Decision 3: fast runtime verification and provisioning E2E are separate jobs

They answer different questions.

**Fast runtime smoke** asks:
- did the current hosted dev instance survive deploy and still behave correctly?

**Provisioning E2E** asks:
- can the control plane create a fresh hosted instance from scratch?

They should not be bundled into one noisy workflow.

### Decision 4: no alias magic in automation

`david.longhouse.ai` is either:

- deleted from automation entirely, or
- reintroduced as an explicit first-class alias managed by the control plane and infra code

This sprint chooses the first option.

## Minimal Control-Plane Contract Changes

The control plane already has most of what we need. The goal is to expose less implicit logic to scripts.

### Extend `InstanceOut`

Add a computed `url` field to the control-plane response model.

Example:

```json
{
  "id": 7,
  "email": "david010@gmail.com",
  "subdomain": "david010",
  "url": "https://david010.longhouse.ai",
  "status": "active"
}
```

This is deliberately small:

- no new resolve endpoint
- no new selector model
- no alias registry
- no new auth metadata

CI and local scripts can call existing `GET /api/instances` or `GET /api/instances/{id}` and use `url` directly.

## Shared Helper Layer

Create a tiny shared hosted helper layer rather than spreading curl logic across workflows.

### Proposed helper file

`scripts/lib/hosted-instance.sh`

Responsibilities:

- fetch instance list from control plane
- resolve an instance by `subdomain`
- emit canonical `INSTANCE_ID` and `INSTANCE_URL`
- mint a login token
- exchange login token for a cookie jar
- optionally trigger reprovision

This file becomes the single shell surface used by:

- `scripts/smoke-prod.sh`
- `scripts/run-prod-e2e.sh`
- `scripts/qa-live.sh`
- manual deploy/reprovision helpers
- GitHub Actions workflows

No new Python CLI is needed unless shell becomes painful.

## Hosted Smoke Redesign

### Inputs

Hosted smoke should take:

- `CONTROL_PLANE_URL`
- `CONTROL_PLANE_ADMIN_TOKEN`
- `INSTANCE_SUBDOMAIN`

Optional:

- `SMOKE_MODE=runtime|full`
- `RUN_LLM=0|1`

### Auth behavior

Hosted smoke should:

1. Resolve the instance from the control plane
2. Hit `{INSTANCE_URL}/api/health`
3. If health says `auth_enabled=false`, run unauthenticated checks only and print that explicitly
4. If health says `auth_enabled=true`, mint login token and authenticate via `accept-token`
5. Run authenticated checks using the cookie jar

### Remove from hosted smoke

- `SMOKE_TEST_SECRET`
- `/api/auth/service-login`
- hardcoded `david.longhouse.ai`
- repo vars for `SMOKE_FRONTEND_URL` / `SMOKE_API_URL`

## Live QA Redesign

`qa-live.sh` should stop scraping `LONGHOUSE_PASSWORD` from the container.

Instead it should:

- default to `INSTANCE_SUBDOMAIN=david010`
- use the same hosted auth helper
- run Playwright using the authenticated browser flow

If the live QA test suite currently requires password login, update it to support the cookie-based hosted auth bootstrap path.

## Production E2E Redesign

`run-prod-e2e.sh` should stop requiring `SMOKE_TEST_SECRET`.

Instead it should:

- resolve by `INSTANCE_SUBDOMAIN`
- authenticate with the hosted auth helper
- pass the authenticated state to Playwright

If Playwright currently assumes password/service-login setup, add one explicit hosted-auth setup step. Do not keep both flows for hosted.

## Deploy-and-Verify Redesign

### New deploy flow

`deploy-and-verify.yml` should do exactly this:

1. Wait for runtime image build
2. Deploy marketing site via Coolify
3. Deploy control plane via Coolify
4. Reprovision `david010` via control-plane admin API
5. Run shared hosted smoke against `david010`
6. Run live QA / prod E2E against `david010`

### Remove from deploy flow

- SSH into `/opt/longhouse/david`
- `docker compose pull && docker compose up -d`
- `CONTAINER_NAME=longhouse-david`
- hardcoded `david.longhouse.ai`

Those reflect an outdated hosted topology.

## CI Lane Split

### Lane A: Hosted Runtime Smoke (fast, every deploy)

Target:
- persistent hosted dev instance: `david010`

Purpose:
- verify the currently running hosted environment after reprovision

Checks:
- health
- auth bootstrap
- key authenticated API checks
- optional lightweight LLM path

### Lane B: Control-Plane Provisioning E2E (slower, nightly/manual)

Target:
- fresh ephemeral instance

Purpose:
- verify create -> health -> auth -> cleanup end-to-end

Use existing direction from `scripts/provision-e2e-live.sh`.

This script already matches the target architecture better than current smoke.

## Implementation Plan

### Phase 1: Canonical URL + shared hosted helper

Deliverables:

- add `url` to control-plane `InstanceOut`
- add `scripts/lib/hosted-instance.sh`
- add tests for `url` in control-plane responses

Acceptance criteria:

- scripts no longer derive hosted URL in multiple places
- shell helpers can resolve `david010` from control-plane data

### Phase 2: Rewrite hosted smoke auth

Deliverables:

- update `scripts/smoke-prod.sh` to use login token + `accept-token`
- remove hosted dependency on `SMOKE_TEST_SECRET`
- make auth-disabled behavior explicit rather than silent

Acceptance criteria:

- hosted smoke passes against `david010` after reprovision with no manual env patching
- hosted smoke no longer calls `/api/auth/service-login`

### Phase 3: Unify live QA and prod E2E

Deliverables:

- update `scripts/qa-live.sh`
- update `scripts/run-prod-e2e.sh`
- update `make verify-prod`

Acceptance criteria:

- password scraping from container is gone for hosted QA
- prod E2E no longer requires `SMOKE_TEST_SECRET`

### Phase 4: Replace workflow drift

Deliverables:

- update `.github/workflows/smoke-after-deploy.yml`
- update `.github/workflows/deploy-and-verify.yml`
- remove stale repo vars and secrets from workflow usage

Acceptance criteria:

- no hosted workflow hardcodes `david.longhouse.ai`
- no hosted workflow assumes compose-managed tenant deploys
- no hosted workflow uses `SMOKE_TEST_SECRET`

### Phase 5: Cleanup and docs

Deliverables:

- remove stale comments and defaults
- update `AGENTS.md` deploy/verify notes if needed
- update any README/docs that still point hosted verification at old hostname or auth flow

Acceptance criteria:

- one documented hosted verification flow remains

## Files Expected to Change

Control plane:

- `apps/control-plane/control_plane/schemas.py`
- `apps/control-plane/control_plane/routers/instances.py`
- `apps/control-plane/tests/test_provisioning_flow.py`

Shared scripts:

- `scripts/lib/hosted-instance.sh` (new)
- `scripts/smoke-prod.sh`
- `scripts/run-prod-e2e.sh`
- `scripts/qa-live.sh`
- `scripts/provision-e2e-live.sh` (only if needed for helper reuse)

Workflows and make targets:

- `.github/workflows/smoke-after-deploy.yml`
- `.github/workflows/deploy-and-verify.yml`
- `Makefile`

Docs/tracking:

- `TODO.md`
- `AGENTS.md` (only if operator guidance changes materially)

## Acceptance Criteria

The sprint is done when all of these are true:

1. No hosted workflow or hosted script hardcodes `david.longhouse.ai`.
2. No hosted smoke or prod E2E path depends on `SMOKE_TEST_SECRET`.
3. `verify-prod` uses the same hosted auth helper as CI smoke.
4. `qa-live` uses the same hosted auth helper as CI smoke.
5. `deploy-and-verify.yml` reprovisions through the control plane instead of SSH + compose.
6. Runtime smoke and provisioning E2E are distinct lanes.
7. OSS local commands and docs still work unchanged.

## Rollout Notes

- Land phases in order.
- Do not build a compatibility layer for old hosted smoke.
- It is acceptable for one PR to temporarily keep `service-login` support in the backend as an internal/testing primitive, but hosted callers should switch immediately.
- Remove GitHub secret and repo var usage only after the new workflows are green.

## Risks

### Risk: Playwright hosted auth setup is harder than shell smoke

Mitigation:
- keep shell smoke first
- add one explicit Playwright setup path for hosted cookie bootstrap
- do not preserve password login as the hosted default just because it already exists

### Risk: control-plane `url` field feels redundant

Mitigation:
- redundancy here is intentional and small
- canonical URL belongs in the control-plane contract, not scattered across scripts

### Risk: temptation to add a bigger instance registry

Mitigation:
- do not add `purpose`, labels, aliases, or environments in this sprint
- use `subdomain` and one persistent target only

## Prior Art Checked

These references inform the direction, but they do not justify making the system more complex than needed:

- OAuth 2.0 Token Exchange / Keycloak token exchange: validates the control-plane-issued short-lived token -> instance-local session pattern.
- GitHub ARC runner scale sets: validates keeping runner orchestration separate from application runtime lifecycle.
- GitHub Actions OIDC: good future path for replacing long-lived CI admin tokens, but not required in this sprint.
- Fly Machines + attached volumes: good mental model for stateful per-instance runtimes behind a control plane.
- Dagger: possible later tool if GitHub YAML drift remains painful after helper unification.
- Coolify API docs: current Coolify has more API surface than the repo's old assumptions suggest, but migration is out of scope here.

## Explicitly Deferred

- GitHub OIDC replacement for `CONTROL_PLANE_ADMIN_TOKEN`
- formal alias support for hosted instances
- richer hosted target selectors beyond `subdomain`
- replacing shell helpers with a typed CLI
- orchestration/platform changes beyond the existing control plane
