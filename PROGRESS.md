# Zerg 0-1 Review Remediation Progress

This document is the source of truth for the remediation plan and status.

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
- [ ] http_request SSRF risk → add URL validation tests + block private/unsafe targets.
- [ ] Title generator uses max_output_tokens → add request-payload test + remove token caps.
- [ ] WorkerRunner timeout docs drift → add doc check or update docs.

## Workstream: Frontend
- [ ] useJarvisClient sets connected without real connection + cached agent fetch only → add hook tests + implement real SSE connect + fetch.
- [ ] useVoice stubbed flow → add hook tests + connect to voiceController (no fake timers).
- [ ] Knowledge Sources “Add Context” no-op → add UI/service test + implement API call.

## Workstream: E2E
- [ ] Core worker flow E2E (spawn_worker) missing → add test + make pass.
- [ ] Workflow logs drawer streaming test skipped → unskip, fix UI/test, make pass.
- [ ] Workflow status indicator test skipped → unskip, fix UI/test, make pass.

## Workstream: Runner / Ops
- [ ] Runner install script uses repo HEAD → add test/verification + switch to versioned release artifact.
- [ ] Runner metadata docker_available always false → add test + detect docker.

## Workstream: Docs
- [ ] Email connector PRD marked completed but TODOs remain → update status language.

## Log
- 2026-01-19: Initialized remediation plan.
