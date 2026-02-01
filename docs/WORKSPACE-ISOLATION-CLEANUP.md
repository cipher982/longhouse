# Workspace Isolation Cleanup Plan

**Date:** 2026-02-01
**Status:** Complete
**Goal:** Clarify workspace isolation (for parallel agent work) vs Docker sandbox (security), remove unused sandbox code

---

## Background

We had two concepts conflated:

1. **Workspace Isolation** (keep) — Each commis gets its own git clone + branch so multiple agents can work on the same codebase without stepping on each other. This is handled by `WorkspaceManager`.

2. **Docker Sandbox** (remove) — Container-based execution for security isolation. Never actually used in production. Adds complexity and Docker dependency.

**Key insight:** OSS users running `pip install longhouse` don't need Docker for security. They're running on their own machine. The workspace isolation (directory-based) gives them what they actually want: clean working directories for parallel agents.

---

## What We're Keeping

### WorkspaceManager (directory-based isolation)

```
~/.longhouse/workspaces/
├── ws-123-abc/          # git clone for commis 1
│   └── (working tree)
├── ws-456-def/          # git clone for commis 2
│   └── (working tree)
└── ...
```

Each workspace commis:
1. Clones repo to isolated directory
2. Creates branch `oikos/{run_id}`
3. Works without affecting other commis
4. Diff captured at end
5. Cleaned up when done

This is the "sandbox" OSS users actually want — clean workspaces, not Docker containers.

---

## What We're Removing

### Docker Sandbox (`sandbox=True`)

| File | What to Remove |
|------|----------------|
| `docker/commis-sandbox/` | Delete entire directory |
| `cloud_executor.py` | `_run_in_container()`, `_kill_container()`, `check_sandbox_available()`, `SANDBOX_IMAGE` |
| `cloud_executor.py` | `sandbox` parameter from `run_commis()` |
| `commis_job_processor.py` | `job_sandbox` logic, `sandbox` param from `_build_hook_env()` |
| `models.py` | Mark `sandbox` column as deprecated (keep for migration safety) |
| `test_cloud_executor.py` | Remove sandbox-related tests |
| `commis-sandbox.spec.ts` | Rename or keep (just tests schema exists) |

---

## Documentation Updates

### VISION.md — Rewrite "Commis Execution Isolation" section

**Before (lines 325-363):** Framed around Docker security isolation, trusted vs untrusted

**After:** Framed around workspace isolation for parallel work

```markdown
## Commis Workspace Isolation

Workspace mode provides directory-based isolation so multiple commis can work on the same codebase simultaneously:

```
✓ Git clone isolation (own directory per commis)
✓ Git branch isolation (oikos/{run_id})
✓ Process group isolation (killable on timeout)
✓ Artifact capture (diff, logs accessible to host)
```

**How it works:**
1. WorkspaceManager clones repo to `~/.longhouse/workspaces/{commis_id}/`
2. Creates working branch `oikos/{commis_id}`
3. Commis executes via `hatch` subprocess
4. Changes captured as git diff
5. Artifacts stored for Oikos to reference
6. Workspace cleaned up (or retained for debugging)

This enables the "multiple agents adding features" pattern without conflicts.
No Docker required — it's just directories and git branches.
```

### AGENTS.md — Minor update

Remove any sandbox references in learnings section.

### config/claude-hooks/README.md — Simplify

Remove `host.docker.internal` references since we're always running on host.

---

## Implementation Order

### Phase 1: Documentation (do first)
1. Update VISION.md — rewrite Commis Execution Isolation section
2. Update AGENTS.md — remove sandbox references
3. Update config/claude-hooks/README.md — simplify

### Phase 2: Codebase Cleanup
1. Delete `docker/commis-sandbox/` directory
2. Simplify `cloud_executor.py`:
   - Remove `_run_in_container()` method (~200 lines)
   - Remove `sandbox` parameter
   - Remove sandbox-related constants
3. Simplify `commis_job_processor.py`:
   - Remove `job_sandbox` variable
   - Simplify `_build_hook_env()` (no sandbox param)
4. Update `models.py`:
   - Add deprecation comment to `sandbox` column
   - Keep column for migration safety (or create migration to drop)
5. Update tests:
   - Remove sandbox tests from `test_cloud_executor.py`
   - Rename `commis-sandbox.spec.ts` to `commis-schema.spec.ts`

### Phase 3: Hooks Cleanup
1. Simplify `deploy-hooks.sh` (remove sandbox considerations)
2. Simplify `_build_hook_env()` to always use localhost

---

## Migration Notes

**The `sandbox` column:**
- Option A: Keep column, add comment "deprecated, always False"
- Option B: Create migration to drop column

Recommend Option A for safety — dropping columns can cause issues if old code references them.

**E2E test `commis-sandbox.spec.ts`:**
- Actually just tests that the schema migration worked
- Rename to `commis-schema.spec.ts` for clarity

---

## Success Criteria

1. `make test` passes
2. `make test-e2e` passes
3. No Docker dependency for workspace commis
4. VISION.md clearly explains workspace isolation
5. `docker/commis-sandbox/` directory deleted
6. `cloud_executor.py` is ~200 lines shorter

---

## Open Questions

1. Should we keep the `sandbox` column or drop it?
2. Should `deploy-hooks.sh` be removed entirely? (one-time setup, maybe just docs)
