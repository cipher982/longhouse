# TODO

Current work only. Completed work → `git log`. Design docs → `docs/specs/` (only for active open tasks).

## Rules

- `TODO.md` is a slim index, not a work journal.
- Substantial active work gets one file under `docs/tasks/open/`.
- When a task finishes, delete the file — git history is the record.
- No backlog section. If it's not active, it doesn't belong here.

## Active

- [AI ops watchman](docs/tasks/open/ai-ops-watchman.md) — AI-first tenant-local monitoring with raw observation capture, Grok 4.1 analysis, durable incidents, email escalation, and explicit input-token cost tracking.
- [Hosted ingest bloat cleanup](docs/tasks/open/hosted-ingest-bloat-cleanup.md) — live `david010` tenant is thrashing on giant repeated Codex ingest; diagnose root cause, stop replay, and add guards/repair.
- [Managed-local Loop tail optimization](docs/tasks/open/managed-local-loop-tail-optimization.md) — `IN PROGRESS (Codex)`; remaining tail is the gap between assistant reply visibility and durable transcript ship, plus a separate cold launch warmup flake after reprovision.
- [Managed-local session control](docs/tasks/open/managed-local-session-control.md) — tmux isolation, failed pane inspection, readiness hook bridge, e2e dogfood.
- [Runtime story simplification](docs/tasks/open/launch-runtime-simplification.md) — Phase 1 done; Phase 2+ is OikosService deletion (3k LOC, needs design).
- [Proactive Oikos operator mode](docs/tasks/open/oikos-proactive-operator.md) — Phase 1 done; Phase 2 in progress.
- [Oikos conversations + Gmail](docs/tasks/open/oikos-conversations-email.md) — Phases 1-9 done; Phase 10 stalled on mailbox infra.
