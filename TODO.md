# TODO

Current work only. Completed work → `git log`. Design docs → `docs/specs/` (only for active open tasks).

## Rules

- `TODO.md` is a slim index, not a work journal.
- Substantial active work gets one file under `docs/tasks/open/`.
- When a task finishes, delete the file — git history is the record.
- No backlog section. If it's not active, it doesn't belong here.

## Active

- [Managed-local drive bugs](docs/tasks/open/managed-local-drive-bugs.md) — Codex no-context + Claude sync dead-end. 5 steps: canary → session SSE stream → split chat semantics → Codex bridge → instrument latency.
- [Runtime story simplification](docs/tasks/open/launch-runtime-simplification.md) — Phase 1 done; Phase 2+ is OikosService deletion (3k LOC, needs design).
- [Proactive Oikos operator mode](docs/tasks/open/oikos-proactive-operator.md) — Phase 1 done; Phase 2 in progress.
- [Oikos conversations + Gmail](docs/tasks/open/oikos-conversations-email.md) — Phases 1-9 done; Phase 10 stalled on mailbox infra.
