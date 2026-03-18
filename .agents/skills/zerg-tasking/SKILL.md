---
name: zerg-tasking
description: Maintain Zerg's slim TODO plus per-task files. Use when creating, updating, archiving, or closing multi-step work.
---

# Zerg Tasking

## Core Rules

- `TODO.md` is a short index of current work only.
- Put substantial active work in exactly one file under `docs/tasks/open/`.
- When a task is done, move it to `docs/tasks/done/YYYY-MM/` if the notes still matter; otherwise delete the task file and let git history carry it.
- Put deferred or speculative work in `docs/tasks/backlog/`, not in the active list.
- Never append long completion logs to `TODO.md`.

## Use A Task File When

- The work will span multiple commits.
- The work has real done conditions, rollout steps, or handoff value.
- Another agent could plausibly continue the task later.

## Do Not Use A Task File When

- The change is a small one-commit fix.
- The work is fully captured by the commit itself.
- The item is really a permanent rule or guardrail, not an active task.

## Workflow

1. Before substantive work, create or update `docs/tasks/open/<slug>.md`.
2. Add one short line to `TODO.md` linking to that file.
3. Keep checklist, status, and notes current while you work.
4. Commit in atomic slices; update the task file when the state actually changes.
5. When done, move the file to `docs/tasks/done/YYYY-MM/` or delete it if archive value is low.

## Minimal Task Template

```md
# Task Title

Status: In progress
Spec: `docs/specs/...`  # optional
Last updated: YYYY-MM-DD

## Goal

One paragraph on what the task is for.

## Done when

- Concrete acceptance condition
- Concrete acceptance condition

## Checklist

- [ ] Step
- [ ] Step

## Notes

- Only decisions, blockers, or verification that matter later
```

## Migration Rule

When cleaning up stale tracking:

- archive the old monolithic snapshot under `docs/tasks/archive/`
- keep only genuinely active tasks in `TODO.md`
- collapse duplicate task entries into one canonical task file
