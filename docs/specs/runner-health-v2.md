# Runner Health V2

Status: Done
Last updated: 2026-03-15

## Goal

Make runner presence, diagnosis, and attention flows trustworthy for three real consumers:
- David operating his own fleet
- hosted single-tenant users paying for Longhouse
- OSS self-installers running one instance for themselves

The current product already has runner install, reconnect, doctor, and repair primitives. The main gap is that raw stored runner status is still treated as truth while the actual liveness signal is `last_seen_at`.

## Problems to Fix

### 1. Heartbeats are not the source of truth

Today:
- the websocket path marks runners `online` on connect
- heartbeats only update `last_seen_at`
- disconnect cleanup marks runners `offline`
- app startup resets stale `online` rows to `offline`

That means an ungraceful runner disappearance can leave a runner stuck `online` forever while `last_seen_at` quietly goes stale.

### 2. Diagnosis is split across product surfaces

Today:
- server-side doctor explains what Longhouse sees
- local CLI doctor explains what the machine sees
- Oikos only has `runner_list` and install-token tools

Oikos can therefore say a runner is unavailable, but it does not automatically have the same repair context as the runner detail page or the local machine.

### 3. Alerts and wakeups are not runner-aware

The repo already has:
- builtin monitoring jobs
- SES-backed alert email
- Telegram delivery
- Oikos wakeup ledger + operator sweep

But runner offline transitions are not wired into any of those paths.

### 4. Capability enrollment is still a footgun

The register endpoint can store capabilities on the runner model, but the request schema currently cannot send them. Product install paths therefore silently fall back to `exec.readonly` unless edited later.

## First Principles

### Health must be derived, not declared

Persisted runner `status` should be treated as:
- an administrative state (`revoked`)
- a cached convenience field for UI and queries

It must not be the sole source of truth for liveness.

The durable liveness signal is:
- `last_seen_at`
- plus the runner-reported heartbeat interval
- plus whether this Longhouse process still has a live websocket connection for the runner

### One health model everywhere

Longhouse needs one shared assessment used by:
- `/api/runners`
- `/api/runners/status`
- `runner_list`
- `runner_exec`
- prompt composition
- server-side doctor
- background reconciliation

### Attention is a policy layer, not a health layer

Runner health should produce incidents and reasons.

Notifications and proactive wakeups should then fan out through existing channels:
- Telegram when linked and available
- alert email when configured on the instance
- Oikos wakeups when operator mode is enabled

### Repair should stay boring

Do not add magic remote healing.

Repair remains:
- restart the local service when obviously down
- otherwise regenerate the repair command and re-run the installer

## Target Product Shape

### Runner health assessment

Add a shared backend service that derives:
- `effective_status`: `online | offline | revoked`
- `status_reason`: stable reason code such as `fresh_heartbeat`, `stale_heartbeat`, `never_connected`, `revoked`
- heartbeat interval + stale-after threshold
- offline duration / last-seen age
- install mode
- reported vs configured capabilities
- runner version vs latest release tag

### Health incidents

Add a durable `RunnerHealthIncident` table to track open offline incidents.

It should support:
- deduped alerting
- deduped Oikos wakeups
- clean resolution when a runner comes back

### Reconciliation job

Add a builtin job running every 2 minutes that:
- assesses all runners
- converges cached DB status with effective health
- opens/closes offline incidents
- sends one external alert after a short threshold
- triggers one Oikos wakeup after a longer threshold

### Richer runner API responses

Runner API responses should expose health fields directly so:
- the Runners page can show honest state
- Oikos `runner_list` can explain offline reasons without a second hidden system

### Better doctor chain of custody

Add a lightweight authenticated runner preflight endpoint so the local CLI doctor can distinguish:
- Longhouse reachable and credentials valid
- wrong instance / missing runner
- stale secret / revoked runner

That closes the biggest diagnosis gap left by `doctor v1`.

### Capability contract

Product install paths should send explicit capabilities during registration.

Decision for V2:
- keep the raw API fallback as `exec.readonly` for backwards compatibility
- default Longhouse product install flows to explicit `exec.full`
- always persist the server-authoritative capabilities returned by registration

## Scope

### In scope

- runner health assessment service
- capability registration fix
- explicit capability enrollment in install flows
- local doctor preflight auth check
- reconciliation + incidents + alerts + wakeups
- Oikos runner doctor surface
- runner UI health/version/recent jobs improvements

### Out of scope

- remote service restarts from the web UI
- binary self-update agent
- multi-instance fleet manager UI
- SSH bridge redesign
- Windows support

## Rollout Order

### Slice 1

- task/spec docs
- capability registration contract
- runner health assessment service
- API/tool/prompt adoption of effective health

### Slice 2

- runner preflight endpoint
- local CLI doctor upgrade
- server-side doctor refresh for version + health reasons

### Slice 3

- reconciliation job
- durable offline incidents
- Telegram/email attention paths
- Oikos wakeups

### Slice 4

- Oikos doctor tool
- runner UI health/version/recent jobs
- final verification and ship

## Progress Log

- 2026-03-14: Initial V2 spec written after validating the current websocket/heartbeat flow, doctor UX, alert helpers, and Oikos wakeup infrastructure in the codebase.
- 2026-03-14: Shipped the health-assessment + enrollment-contract slice, including explicit capability registration, derived health responses, and heartbeat interval reporting from the runner hello payload.
- 2026-03-14: Shipped the doctor chain-of-custody slice: unauthenticated `/api/runners/preflight`, local doctor credential validation, and server-side doctor version/offline reasoning.
- 2026-03-14: Shipped the reconciliation slice: durable `RunnerHealthIncident`, builtin `runner-health-reconcile` job, deduped Telegram/email alerts, and deduped Oikos wakeups for prolonged outages.
- 2026-03-14: Shipped the UI slice: Runners and runner detail pages now surface effective health reasons, heartbeat windows, version drift, capability sync state, and recent jobs.
- 2026-03-14: Local verification passed for `make test-runner-unit`, frontend typecheck/lint, `make test`, and `make test-e2e`.
- 2026-03-15: Final ship is complete. Runtime build `23099091822` and deploy workflow `23099120591` shipped the CSS/build fix needed to keep the new runner UI in the production bundle, and CI run `23099226637` finished green after the Gmail onboarding harness was updated for the new `/api/auth/methods` contract and a transient `glm-4.7-flash` model-smoke timeout was rerun.
- 2026-03-15: Live hosted verification passed: `make qa-live` succeeded 8/8, `/api/health` on `david010.longhouse.ai` was healthy, and the production `/api/runners` payload showed heartbeat-derived status summaries for the real fleet with stale canaries no longer pretending to be online.
