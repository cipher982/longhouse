# Task Files

## Structure

- `TODO.md` — current work only; keep it short.
- `docs/tasks/open/` — one file per substantial active task.
- `docs/tasks/done/YYYY-MM/` — completed task files worth preserving.
- `docs/tasks/archive/` — historical dumps and pre-refactor snapshots.
- `docs/tasks/backlog/` — deferred or speculative work that is not in the active queue.

## When To Create A Task File

- The work will span multiple commits.
- The work has real done conditions or rollout steps.
- Another agent could plausibly pick it up later.

## When Not To

- The change is a one-commit fix with obvious scope.
- Git history already captures everything useful.
- The “task” is really just a permanent guardrail or rule.

## Workflow

1. Create or update the task file in `docs/tasks/open/`.
2. Add one short line to `TODO.md`.
3. Keep checklist and notes current while you work.
4. When done, move the task file to `docs/tasks/done/YYYY-MM/` or delete it if the archive value is low.
5. Never append long completion notes to `TODO.md`.
