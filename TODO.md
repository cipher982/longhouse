# TODO

Current work only. Historical task logs live in [docs/tasks/archive/todo-history-2026-03-17.md](docs/tasks/archive/todo-history-2026-03-17.md).

## Rules

- `TODO.md` is a slim index, not a work journal.
- Substantial active work lives in one file under `docs/tasks/open/`.
- When a task finishes, move its file to `docs/tasks/done/YYYY-MM/` if the notes are worth keeping; otherwise delete it and let git history carry it.
- Deferred or speculative work belongs in `docs/tasks/backlog/`, not the active list.

## Active

- [Realtime Timeline desktop control view](docs/tasks/open/timeline-realtime-action-center.md) — make unmanaged local honest, add execution-home visibility, strengthen managed-local runtime truth, then finish the remaining Timeline cleanup and SSE hot-path work.
- [Managed-local Loop profiling](docs/tasks/open/managed-local-loop-profiling.md) — persist the turn-review latency trail, surface it in Session Detail, and prove the post-fix hot-path timing with a real smoke run.
- [Frontend effect-boundary cleanup](docs/tasks/open/frontend-effect-boundary-cleanup.md) — rewrite the worst effect-driven state choreography so pages are easier to reason about and safer to change.
- [Mobile Loop Inbox](docs/tasks/open/mobile-loop-inbox.md) — ship the tiny phone-first approve/continue surface instead of forcing the desktop session UI onto mobile.
- [Memory system consolidation](docs/tasks/open/memory-system-consolidation.md) — ship the Memory Files cleanup, reprovision, and verify hosted behavior.
- [Runtime story simplification](docs/tasks/open/launch-runtime-simplification.md) — finish the deletion path for the current Oikos harness so the product story matches the code.
- [Oikos conversations + Gmail launch](docs/tasks/open/oikos-conversations-email.md) — finish the remaining Gmail canaries and retire the last compatibility-only conversation/history path.
- [Proactive Oikos operator mode](docs/tasks/open/oikos-proactive-operator.md) — ship one bounded autonomy slice without building a giant automation engine.
- [Engine shipper byte batching](docs/tasks/open/shipper-byte-batching.md) — make oversized session deltas progress via exact byte-range batching.
- [Compaction fidelity + active context semantics](docs/tasks/open/compaction-fidelity.md) — close the remaining compaction/noise semantics work.
- [Codex/Gemini continuation parity](docs/tasks/open/codex-gemini-continuation-parity.md) — make cloud continuation true beyond Claude.
- [Runner onboarding hardening](docs/tasks/open/runner-onboarding-hardening.md) — finish the real-machine proof ring and final launch checks.

## Deferred / Backlog

- [Oikos dispatch contract research](docs/tasks/backlog/oikos-dispatch-contract.md) — useful, but not current launch-path work.

## Archive

- Historical monolithic task log: [docs/tasks/archive/todo-history-2026-03-17.md](docs/tasks/archive/todo-history-2026-03-17.md)
- Completed task files worth keeping: `docs/tasks/done/2026-03/`
