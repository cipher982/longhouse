# TODO

Current work only. Historical task logs live in [docs/tasks/archive/todo-history-2026-03-17.md](docs/tasks/archive/todo-history-2026-03-17.md).

## Rules

- `TODO.md` is a slim index, not a work journal.
- Substantial active work lives in one file under `docs/tasks/open/`.
- When a task finishes, move its file to `docs/tasks/done/YYYY-MM/` if the notes are worth keeping; otherwise delete it and let git history carry it.
- Deferred or speculative work belongs in `docs/tasks/backlog/`, not the active list.

## Close Out (done or ~15 min to finish)

- [Compaction fidelity](docs/tasks/open/compaction-fidelity.md) — one decision left: formalize `progress` event handling, add a test, done.
- [Memory system consolidation](docs/tasks/open/memory-system-consolidation.md) — code complete; Phase 4 deploy steps remain (push, CI, reprovision, verify).
- [Runner onboarding hardening](docs/tasks/open/runner-onboarding-hardening.md) — 95% done; workflow dispatch run, persistence sufficiency decision, mobile spot checks.
- [Mobile loop inbox](docs/tasks/open/mobile-loop-inbox.md) — code complete; needs live dogfood of push notification flow with VAPID keys on instance.

## Active

- [Managed-local Loop tail optimization](docs/tasks/open/managed-local-loop-tail-optimization.md) — `IN PROGRESS (Codex)`; use the persisted review timings to shave the remaining pre-enqueue + claim latency now that correctness is green.
- [Managed-local session control](docs/tasks/open/managed-local-session-control.md) — dedicated tmux server isolation, failed pane inspection, readiness hook bridge, end-to-end dogfood.
- [Runtime story simplification](docs/tasks/open/launch-runtime-simplification.md) — copy/narrative done; remaining work is the OikosService/react_engine deletion path (3k LOC, 36+ call sites).
- [Proactive Oikos operator mode](docs/tasks/open/oikos-proactive-operator.md) — Phase 1 complete (wakeups, policy, ledger, shadow journeys); Phase 2 in progress (broader actions, browser/hosted smokes).
- [Oikos conversations + Gmail launch](docs/tasks/open/oikos-conversations-email.md) — Phases 1-9 done; Phase 10 stalled: real Gmail canaries (OSS, hosted, cross-browser) need mailbox infra.
- [Realtime Timeline desktop control view](docs/tasks/open/timeline-realtime-action-center.md) — Phases 1-2 done (honest labels, execution-home); Phases 3-4 remain (managed runtime class, SSE poll cleanup).
- [Codex/Gemini continuation parity](docs/tasks/open/codex-gemini-continuation-parity.md) — architecture ready; needs provider-specific resume builders, output parsers, session state reconstruction.

## Backlog

- [Shipper byte batching](docs/tasks/open/shipper-byte-batching.md) — functionally complete and shipping; only remaining work is large-session fixture tests for CI confidence.
- [Oikos dispatch contract research](docs/tasks/backlog/oikos-dispatch-contract.md) — useful, but not current launch-path work.

## Archive

- Historical monolithic task log: [docs/tasks/archive/todo-history-2026-03-17.md](docs/tasks/archive/todo-history-2026-03-17.md)
- Completed task files worth keeping: `docs/tasks/done/2026-03/`
