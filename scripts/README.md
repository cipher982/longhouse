# scripts/ — Reference Index

Most scripts here are invoked via `make` targets — prefer `make X` over running scripts directly. This README exists so agents can quickly find the right script for a task.

**Other script locations:**
- `server/scripts/` — backend Python utilities (build_demo_db, export_openapi, debug_trace, etc.)
- `e2e/scripts/` — Playwright helpers (gpu-profiler, profile-landing, provider-continuation-smoke)

---

## Quick Reference

| Task | Script / Command |
|------|-----------------|
| Start local dev | `make dev` → `scripts/dev.sh` |
| Run unit tests | `make test` |
| Run core E2E | `make test-e2e` |
| QA against live instance | `make qa-live` → `scripts/qa-live.sh` |
| Full OSS QA (clone + unit + E2E) | `make qa-oss` → `scripts/qa-oss.sh` |
| Smoke prod (API + auth + LLM) | `make smoke-prod` → `scripts/smoke-prod.sh` |
| Generate WebSocket types | `make generate-ws-types` → `scripts/generate-ws-types-modern.py` |
| Generate SSE types | `make generate-sse-types` → `scripts/generate-sse-types.py` |
| Debug a hosted instance | `scripts/hosted-loop-debug.sh <subdomain>` |
| Check Codex bridge E2E | `make test-codex-bridge-e2e` → `scripts/test-codex-bridge-e2e.sh` |

---

## Development

| Script | Purpose |
|--------|---------|
| `dev.sh` | Native SQLite dev server — sets DATABASE_URL, FERNET_SECRET, starts backend + frontend |
| `dev-demo.sh` | Dev with seeded demo database (`make dev-demo`) |
| `dev-docker.sh` | Legacy Docker+Postgres dev (CI/Postgres-specific testing only) |
| `stop-docker.sh` | Stop Docker dev services |
| `install-claude-shim.sh` | Install Claude shim for local development workspace |

---

## Installers (end-user facing)

| Script | Purpose |
|--------|---------|
| `install.sh` | Longhouse one-liner installer (`curl \| bash`) — sets up launchd/systemd, engine, hooks |
| `install-runner.sh` | Runner daemon installer for user infrastructure — WebSocket service, systemd |

---

## Code Generation

> Never edit generated files in `server/zerg/generated/`, `server/zerg/tools/generated/`, or `web/src/generated/` — run these instead.

| Script | Purpose |
|--------|---------|
| `generate-ws-types-modern.py` | Generate WebSocket contract types from `schemas/ws-protocol-asyncapi.yml` |
| `generate-sse-types.py` | Generate SSE event types from `schemas/sse-events.asyncapi.yml` |
| `generate_tool_types.py` | Generate Python types from `schemas/tools.yml` |
| `generate_voiceover.py` | Generate TTS voiceover audio for video scenarios (OpenAI TTS) |
| `generate-production-secrets.py` | One-time setup: generate Fernet + JWT secrets for a fresh deployment |

---

## Testing & QA

| Script | Purpose | When to run |
|--------|---------|------------|
| `qa-live.sh` | 10 live tests against hosted instance (~5s) | After every deploy |
| `qa-oss.sh` | Full OSS QA: fresh clone + unit + E2E + UI gate | Pre-release |
| `smoke-prod.sh` | Prod smoke: public + auth + LLM against hosted instance | After deploy |
| `smoke_models.py` | Smoke-test every model in `config/models.json` | When models config changes |
| `run-prod-e2e.sh` | Run E2E tests against a live hosted instance | Live regression |
| `run-vibetest.sh` | LLM-powered browser QA agents (advisory, non-deterministic) | Exploratory QA |
| `run-visual-analysis.sh` | Screenshot + AI visual analysis | UI regressions |
| `visual-compare.ts` | Pixelmatch + Gemini LLM triage between baseline and current screenshots | Visual diffs |
| `run-readme-tests.py` | Execute contract test blocks embedded in Markdown files | CI + `make test-readme` |
| `lint-test-patterns.sh` | Check for test anti-patterns (waitForTimeout, window.confirm, etc.) | Pre-commit |
| `validate-asyncapi.sh` | Validate AsyncAPI schema | Schema changes |
| `validate-rgba.sh` | Validate RGBA color values in CSS/config | UI color changes |
| `validate-deployment.py` | Deployment pre-flight checklist | Before deploying |
| `verify-single-react.mjs` | Assert only one React instance in the frontend bundle | Bundle audits |
| `verify_real_claude_transcript_fidelity.py` | Fidelity test: real Claude transcript parsing accuracy | Shipper changes |
| `provision-e2e-live.sh` | Live provisioning smoke: admin creates instance → Docker → health → SSO → cleanup | Provisioner changes |

---

## E2E Test Helpers

Called by `make test-*` targets — rarely run directly.

| Script | Purpose |
|--------|---------|
| `shipper-e2e-prereqs.sh` | Run migrations + table check before shipper E2E |
| `shipper-smoke-test.sh` | Shipper live smoke test (requires backend running) |
| `test-hooks-e2e.sh` | E2E test for hook outbox pipeline |
| `test-codex-bridge-e2e.sh` | Codex bridge E2E: start → send → continue → interrupt → cleanup |
| `test-zerg-ops.sh` | Backup/restore retention contract test for `zerg-ops.sh` |
| `runner-vm-canary.sh` | Disposable VM runner canary against hosted instance |
| `runner-vm-canary-host.sh` | Runner VM canary host-side variant |

---

## Production Ops

> `zerg-ops.sh` runs on the **zerg server** (deployed to `/usr/local/bin/zerg-ops`). Others run locally against the hosted API.

| Script | Purpose |
|--------|---------|
| `hosted-loop-debug.sh` | Debug hosted tenant: resolves via control plane, auth cookie, loop-inbox + turn-reviews, then SQLite fallback. **Start here for hosted instance debugging.** |
| `zerg-ops.sh` | Backup/restore retention + offsite sync + Docker prune for zerg server |
| `check-cp-credentials.sh` | Validate Stripe and SES credentials (exit 0/1) — run before control-plane deploy |
| `check-email-routing.sh` | Verify mailto: links have Cloudflare email forwarding rules |
| `control-plane-coolify-contract.sh` | Assert control-plane Coolify config matches expected contracts |
| `deploy-hooks.sh` | Webhook setup / hook deployment |
| `run-onboarding-funnel.sh` | Run onboarding funnel from README contract (fresh clone) |

---

## Managed-Local Agent Tools

Stress and profiling tools for the managed-local feature (Claude/Codex TUI sessions launched by Longhouse). Used for perf profiling and regression testing, not in regular CI.

| Script | Purpose |
|--------|---------|
| `stress_claude_tmux.py` | Stress-test Claude Code turn submission through tmux |
| `stress_codex_tmux.py` | Stress-test Codex turn submission through tmux |
| `managed_local_claude_stress.py` | Full managed-local Claude stress test (launch + turns + teardown) |
| `managed_local_codex_launch_profile.py` | Codex launch profiling — measures time-to-ready |
| `probe_managed_local_claude_tmux.py` | Probe Claude tmux session lifecycle in detail |
| `hosted-managed-local-claude-stress.sh` | Hosted variant: stress-test managed-local Claude on live instance |
| `hosted-managed-local-codex-stress.sh` | Hosted variant: stress-test managed-local Codex on live instance |
| `hosted-managed-local-codex-launch-profile.sh` | Hosted variant: Codex launch profiling on live instance |

---

## Marketing & UI

| Script | Purpose |
|--------|---------|
| `capture_marketing.py` | Manifest-driven marketing screenshot capture (Playwright) — reads `screenshots.yaml` |
| `marketing-screenshots.sh` | Wrapper: runs Playwright screenshot capture (`make marketing-screenshots`) |
| `screenshots.yaml` | Page manifest for `capture_marketing.py` |
| `ui-capture.ts` | Capture local dev UI debug bundle with a11y tree + screenshot (`bunx tsx scripts/ui-capture.ts`) |
| `recolor.py` | Recolor palette utility for UI assets |

---

## Maintenance / One-off Tools

Useful when the specific situation arises, but not in regular rotation.

| Script | Purpose |
|--------|---------|
| `backfill_embeddings.py` | Batched OpenAI embedding backfill for existing sessions without embeddings |
| `fix_codex_orphan_sessions.py` | Fix orphan sessions from incremental-parse session_id bug (dry-run by default) |
| `debug_chat_cli.py` | CLI debug tool for testing session continuation flow locally |
| `check-ports.sh` | Check port availability (diagnostic) |
| `check-db-commits.sh` | Pre-commit helper: warn on new `db.commit()` in router files |

---

## CI Infrastructure (`ci/`)

Called by GitHub Actions workflows — not for direct use.

| Script | Purpose |
|--------|---------|
| `ci/run-on-ci.sh` | CI entry point for named test suites (`--help` for allowlisted suites) |
| `ci/installer-first-run.sh` | Disposable first-run installer smoke in temp HOME |
| `ci/provision-e2e.sh` | Provision local CI instance via control plane + smoke checks |
| `ci/export-hosted-instance-env.sh` | Export hosted instance environment variables for CI |
| `ci/fixtures/` | Test fixture data for CI |

---

## Shared Libraries (`lib/`)

| Script | Purpose |
|--------|---------|
| `lib/hosted-instance.sh` | Shared bash library: resolve a hosted instance via control plane, auth, and API helpers. Sourced by `hosted-loop-debug.sh`, `ci/provision-e2e.sh`, and others. |
