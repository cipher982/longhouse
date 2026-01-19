# Email Connector TODO Remediation Progress

This document is the source of truth for completing the remaining Email Connector TODOs listed in `docs/completed/email_connector_prd.md`.

## Goals
- Add/verify tests for each TODO item.
- Implement fixes or refactors when tests reveal gaps.
- Commit each item separately.

## Status Legend
- [ ] Not started
- [~] In progress
- [x] Done
- [!] Blocked

## Workstream: Email Connector TODOs
- [x] Pub/Sub Gmail flow: connector stores `emailAddress`, Pub/Sub webhook mapping, topic-based watch registration.
- [x] Connector-level watch renewal service: renew expiring watches and persist updated metadata.
- [x] Observability metrics/gauges: history_id + watch_expiry updates, Pub/Sub processing counters.
- [x] Test hygiene: scope `raise_server_exceptions=False` only where needed; connectors API tests present.
- [x] Clean legacy EmailTriggerService: remove stub poller and update references.
- [x] Docs + config updates: `.env.example` + webhook retry semantics + Pub/Sub notes.

## Log
- 2026-01-19: Initialized Email Connector TODO remediation plan.
- 2026-01-19: Added Pub/Sub watch + emailAddress test coverage for Gmail connect flow.
- 2026-01-19: Added watch renewal service test coverage for expiring Gmail connectors.
- 2026-01-19: Added Gmail history_id metric update test coverage.
- 2026-01-19: Scoped unauthenticated TestClient exception handling with fixture tests.
- 2026-01-19: Removed legacy EmailTriggerService and cleaned references with guard test.
- 2026-01-19: Documented Pub/Sub settings/async semantics and added PUBSUB_SA_EMAIL to env example.

---

# Archive: Zerg 0-1 Review Remediation Progress

This section preserves the prior remediation record for reference.

## Goals
- Add a failing test for each confirmed issue.
- Implement the fix.
- Re-run tests to confirm.
- Commit each issue separately.

## Status Legend
- [ ] Not started
- [~] In progress
- [x] Done
- [!] Blocked

## Workstream: Backend
- [x] Worker artifact index updates are non-atomic → add locking + concurrency test.
- [x] Worker ID collisions on same-second + same-task → add deterministic test + fix ID generation.
- [x] http_request SSRF risk → add URL validation tests + block private/unsafe targets.
- [x] Title generator uses max_output_tokens → add request-payload test + remove token caps.
- [x] WorkerRunner timeout docs drift → add doc check or update docs.

## Workstream: Frontend
- [x] useJarvisClient sets connected without real connection + cached agent fetch only → add hook tests + implement real SSE connect + fetch.
- [x] useVoice stubbed flow → add hook tests + connect to voiceController (no fake timers).
- [x] Knowledge Sources “Add Context” no-op → add UI/service test + implement API call.

## Workstream: E2E
- [x] Core worker flow E2E (spawn_worker) missing → add test + make pass.
- [x] Workflow logs drawer streaming test skipped → unskip, fix UI/test, make pass.
- [x] Workflow status indicator test skipped → unskip, fix UI/test, make pass.

## Workstream: Runner / Ops
- [x] Runner install script uses repo HEAD → add test/verification + switch to versioned release artifact.
- [x] Runner metadata docker_available always false → add test + detect docker.

## Workstream: Docs
- [x] Email connector PRD marked completed but TODOs remain → update status language.

## Log
- 2026-01-19: Initialized remediation plan.
- 2026-01-19: Implemented user_text knowledge sources end-to-end with API + UI tests.
- 2026-01-19: Updated WorkerRunner timeout docstring to match enforced behavior.
- 2026-01-19: Added docker availability detection in runner metadata with unit tests.
- 2026-01-19: Switched install-runner to versioned release tarballs with validation + script tests.
- 2026-01-19: Clarified email connector PRD as partially complete.
- 2026-01-19: Added core worker flow E2E and fixed workflow logs/status E2E with WS config + status polling.
