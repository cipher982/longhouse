# Docs Retention Prune (2026-03)

Status: In progress.

## Executive Summary

Longhouse has accumulated too many Markdown docs that duplicate the code, repeat completed implementation work, or preserve session-by-session historical context that git history already stores.

The target is to cut the active doc surface by about 80% and keep only the small set of documents that still act as canonical human-facing contracts.

This sprint treats docs as one of three kinds:

1. Canonical docs that express product intent or external/operator contracts.
2. Historical derivations of shipped code that should live in git history, not the working tree.
3. Transient work artifacts (handoffs, worklogs, generated patrol reports) that should be deleted.

## Decision Log

### Decision: Git history is the archive
Context: Most of the current docs are completed specs, handoffs, and worklogs.
Choice: Delete them outright instead of moving them into a new archive folder.
Rationale: Moving low-value docs into `docs/archive/` preserves clutter while pretending it is cleanup. Git already preserves the history.
Revisit if: We later need an explicit exportable design-history bundle for external users.

### Decision: Keep only docs with a clear source-of-truth role
Context: The repo mixes product docs, operator docs, and historical implementation notes.
Choice: Keep only docs that are still the best place to learn or operate the current system.
Rationale: If the code or AGENTS/README already expresses the truth, a second derivation should be removed.
Revisit if: A deleted area becomes externally supported and needs durable standalone docs.

### Decision: Do not optimize for exact 80.0%
Context: The user asked to slim docs down by 80%.
Choice: Target a final active set of roughly 8-10 canonical docs from the current 44 markdown files.
Rationale: The goal is sharp reduction, not hitting a round number at the cost of deleting something still useful.
Revisit if: The retained set still feels obviously bloated after the prune.

## Inventory Snapshot

Current markdown inventory (excluding vendored `node_modules`, build output, and archived directories): 44 files.

### Planned Keep Set

These survive because they are still canonical:

- `README.md`
- `VISION.md`
- `AGENTS.md`
- `TODO.md`
- `apps/control-plane/README.md`
- `apps/control-plane/API.md`
- `apps/runner/README.md`
- `apps/zerg/backend/README.md`
- `docs/specs/docs-retention-prune.md`

### Planned Delete Set

Delete these classes of docs:

- `docs/handoffs/*`
- `docs/plans/*`
- `docs/specs/*` except this prune spec
- `apps/zerg/backend/docs/*`
- `apps/zerg/backend/docs/specs/*`
- `docs/install-guide.md`
- `scripts/ci/README.md`
- `apps/sauron/README.md`
- `apps/zerg/backend/TESTING_STRATEGY.md`
- `patrol/reports/*.md`

### Explicit Non-Goals

- Do not delete skill definitions under `apps/zerg/backend/zerg/skills/bundled/*/SKILL.md`; those are product/runtime assets, not repo docs.
- Do not create a replacement archive tree.
- Do not rewrite historical docs before deleting them.

## End-State Rules

After the prune:

- `README.md` is the main human entry point.
- `VISION.md` is the main product-intent document.
- `AGENTS.md` is the execution contract for agents.
- `TODO.md` is the live work queue.
- Per-app docs remain only where they define a real external contract.
- Completed specs, handoffs, worklogs, and research notes are removed from the working tree.

## Implementation Phases

### Phase 1: Commit the retention policy
Acceptance criteria:
- This spec exists and is committed.
- The keep/delete rules are explicit.

### Phase 2: Remove transient and historical docs
Acceptance criteria:
- `docs/handoffs/`, `docs/plans/`, and `patrol/reports/*.md` are gone.
- Completed shipped specs under `docs/specs/` and `apps/zerg/backend/docs/specs/` are gone.

### Phase 3: Consolidate and delete duplicated operator docs
Acceptance criteria:
- `docs/install-guide.md`, `scripts/ci/README.md`, `apps/sauron/README.md`, `apps/zerg/backend/TESTING_STRATEGY.md`, and backend doc drift files are removed.
- `README.md` / `AGENTS.md` are updated where they still need tiny durable guidance.

### Phase 4: Review and verify
Acceptance criteria:
- The remaining docs match the planned keep set.
- Repo references to deleted docs are removed or replaced.
- Final markdown count is roughly 8-10 files.

## Verification

- `rg --files -g '*.md' -g '!**/node_modules/**' -g '!**/dist/**' -g '!**/.venv/**' -g '!**/coverage/**' -g '!archive/**'`
- `rg -n 'docs/|README.md|VISION.md|API.md|TESTING_STRATEGY.md|install-guide.md|supervisor_tools.md' AGENTS.md README.md apps scripts docs`
- `git diff --stat`
