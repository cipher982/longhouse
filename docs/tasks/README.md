# Task Files

## Structure

- `TODO.md` — current work only; keep it short.
- `docs/tasks/open/` — one file per substantial active task.
- `docs/specs/` — design docs, only for active open tasks. Delete when the task ships.
- `docs/tasks/archive/` — historical dumps (rare).

## When To Create A Task File

- The work will span multiple commits.
- The work has real done conditions or rollout steps.
- Another agent could plausibly pick it up later.

## When Not To

- The change is a one-commit fix with obvious scope.
- Git history already captures everything useful.

## Workflow

1. Create or update the task file in `docs/tasks/open/`.
2. Add one short line to `TODO.md`.
3. Keep checklist current while you work.
4. When done, delete the task file and its spec. Git history is the record.
