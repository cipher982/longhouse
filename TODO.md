# TODO

Current work only. Historical task logs live in [docs/tasks/archive/todo-history-2026-03-17.md](docs/tasks/archive/todo-history-2026-03-17.md).

## Rules

- `TODO.md` is a slim index, not a work journal.
- Substantial active work lives in one file under `docs/tasks/open/`.
- When a task finishes, move its file to `docs/tasks/done/YYYY-MM/` if the notes are worth keeping; otherwise delete it and let git history carry it.
- Deferred or speculative work belongs in `docs/tasks/backlog/`, not the active list.

## Active

- [Managed-local Loop tail optimization](docs/tasks/open/managed-local-loop-tail-optimization.md) — `IN PROGRESS (Codex)`; use the persisted review timings to shave the remaining pre-enqueue + claim latency now that correctness is green.
- [Managed-local session control](docs/tasks/open/managed-local-session-control.md) — dedicated tmux server isolation, failed pane inspection, readiness hook bridge, end-to-end dogfood.
- [Runtime story simplification](docs/tasks/open/launch-runtime-simplification.md) — copy/narrative done; remaining work is the OikosService/react_engine deletion path (3k LOC, 36+ call sites).
- [Proactive Oikos operator mode](docs/tasks/open/oikos-proactive-operator.md) — Phase 1 complete (wakeups, policy, ledger, shadow journeys); Phase 2 in progress (broader actions, browser/hosted smokes).
- [Oikos conversations + Gmail launch](docs/tasks/open/oikos-conversations-email.md) — Phases 1-9 done; Phase 10 stalled: real Gmail canaries (OSS, hosted, cross-browser) need mailbox infra.

- [Codex/Gemini continuation parity](docs/tasks/open/codex-gemini-continuation-parity.md) — architecture ready; needs provider-specific resume builders, output parsers, session state reconstruction.

## Backlog

- [Managed-local turn ledger phase 2](docs/tasks/backlog/managed-local-turn-ledger-phase-2.md) — route reads terminal + durability from the ledger; phase 1 shipped in [docs/tasks/done/2026-03/managed-local-turn-ledger.md](docs/tasks/done/2026-03/managed-local-turn-ledger.md).
- [Shipper byte batching](docs/tasks/open/shipper-byte-batching.md) — functionally complete and shipping; only remaining work is large-session fixture tests for CI confidence.
- [Oikos dispatch contract research](docs/tasks/backlog/oikos-dispatch-contract.md) — useful, but not current launch-path work.

## Archive

- Historical monolithic task log: [docs/tasks/archive/todo-history-2026-03-17.md](docs/tasks/archive/todo-history-2026-03-17.md)
- Completed task files worth keeping: `docs/tasks/done/2026-03/`
