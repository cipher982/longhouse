# Runner Health V2 Tasks

Status: In progress
Spec: `docs/specs/runner-health-v2.md`
Last updated: 2026-03-14

## Phase 0: Spec and task framing

- [x] Add `TODO.md` tracking entry
- [x] Write V2 runner health spec
- [x] Write granular task checklist
- [ ] Commit Phase 0 artifacts

## Phase 1: Health truth and enrollment contract

- [x] Fix `RunnerRegisterRequest` to accept capabilities
- [x] Make `/api/runners/register` persist requested capabilities for new runners
- [x] Make Longhouse install flows send explicit capabilities during registration
- [x] Add shared runner health assessment service
- [x] Use effective health in runner list/status responses
- [x] Use effective health in `runner_list`, `runner_exec`, and prompt composition
- [x] Add backend tests for health assessment and capability enrollment
- [ ] Commit Phase 1

## Phase 2: Doctor chain of custody

- [x] Add authenticated runner preflight endpoint
- [x] Upgrade local `longhouse-runner doctor` to verify credentials against the configured instance
- [x] Refresh server-side doctor to surface version drift and health-derived reasons
- [x] Add backend/Bun tests for preflight and doctor upgrades
- [ ] Commit Phase 2

## Phase 3: Reconciliation, incidents, and attention

- [x] Add durable `RunnerHealthIncident` model
- [x] Add builtin runner health reconciliation job (every 2 minutes)
- [x] Open/resolve offline incidents from effective health
- [x] Mark cached runner status offline when heartbeats go stale
- [x] Send deduped Telegram or email alerts for prolonged offline incidents
- [x] Trigger deduped Oikos wakeups for prolonged offline incidents
- [x] Add backend tests for reconciliation, alerts, and wakeups
- [ ] Commit Phase 3

## Phase 4: Oikos and UI

- [x] Expose a runner doctor tool to Oikos
- [x] Improve `runner_list` output with health/version context
- [x] Add runner recent-jobs API route
- [ ] Update Runners page with honest health + version data
- [ ] Update runner detail with richer health/version/recent jobs
- [ ] Regenerate OpenAPI frontend types
- [ ] Run targeted frontend validation
- [ ] Commit Phase 4

## Phase 5: Verification and ship

- [ ] Run targeted backend tests
- [ ] Run runner Bun tests
- [ ] Run broader verification (`make test` and any necessary follow-on checks)
- [ ] Push `main`
- [ ] Wait for CI/build workflows to finish successfully
- [ ] Deploy hosted surfaces if needed
- [ ] Reprovision the `david010` instance
- [ ] Verify health and live runner behavior
- [ ] Update this task doc with final notes
- [ ] Commit any final doc/status updates

Notes:
- 2026-03-14: The central V2 decision is to treat heartbeat freshness as runner liveness truth and use DB `status` only as a reconciled cache plus the `revoked` administrative state.
- 2026-03-14: Health truth now also treats live websocket presence as an availability requirement. Fresh heartbeats without a live connection are surfaced as `disconnected_recently` rather than pretending the runner is available.
