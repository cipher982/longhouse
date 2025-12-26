# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---

# Pre-commit Overhaul Spec

**Goal:** Make pre-commit a robust guardrail for AI agents working on the codebase.

**Philosophy:** AI agents commit frequently. Pre-commit must be:
1. **Fast** (< 15s) — agents shouldn't wait minutes
2. **Comprehensive** — catch type errors, broken imports, contract drift
3. **Auto-fixing** — format issues should fix themselves, not block

---

## Current State

| Issue | Impact |
|-------|--------|
| Pre-commit references dead paths (`backend/`, `frontend/`, `asyncapi/`) | Hooks fail or no-op |
| Rust hooks still present | Rust frontend is gone |
| Uses npm instead of bun | Wrong package manager |
| No Python type checking | AI's #1 error type goes uncaught |
| 407 ruff lint errors in backend | Would block all commits if enforced |
| 72 unformatted Python files | Would block all commits if enforced |

---

## Target Architecture

### Tier 1: Pre-commit (< 15s, runs every commit)

| Hook | Purpose | Mode |
|------|---------|------|
| **trailing-whitespace** | Basic hygiene | Block |
| **end-of-file-fixer** | Basic hygiene | Auto-fix |
| **check-json** | Catch malformed JSON | Block |
| **check-yaml** | Catch malformed YAML | Block |
| **ruff** (lint) | Python lint | Auto-fix |
| **ruff-format** | Python formatting | Auto-fix |
| **TypeScript types** | Frontend type safety | Block |
| **ESLint** | Frontend lint | Auto-fix |
| **Prettier** | Frontend formatting | Auto-fix |
| **WS contract drift** | Catch schema mismatches | Block |

**Total estimated time:** 8-12 seconds

### Tier 2: Make validate (< 30s, run before push)

Everything in Tier 1, plus:
- Full contract validation (`bun run validate:contracts`)
- Makefile structure validation

### Tier 3: CI (full test suite)

Everything in Tier 2, plus:
- Backend tests (3min)
- Frontend tests (3s)
- E2E tests (when applicable)

---

## Implementation Plan

### Phase 1: Baseline Cleanup (prerequisite)

Before enabling enforcement, fix existing violations:

1. **Fix ruff format violations** (72 files)
   ```bash
   cd apps/zerg/backend && uv run ruff format zerg/
   ```

2. **Fix ruff lint violations** (407 errors, auto-fixable: 297)
   ```bash
   cd apps/zerg/backend && uv run ruff check --fix zerg/
   # Review remaining ~110 manual fixes
   ```

3. **Commit baseline fix** — one commit to establish clean baseline

### Phase 2: New Pre-commit Config

Replace `.pre-commit-config.yaml` with minimal, working hooks:

```yaml
repos:
  # Generic file hygiene
  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.6.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-json
      - id: check-yaml
        args: [--allow-multiple-documents]
      - id: check-added-large-files
        args: [--maxkb=500]

  # Python: ruff for lint + format (fast, replaces black/isort/flake8)
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.8.4
    hooks:
      - id: ruff
        args: [--fix, --exit-non-zero-on-fix]
        files: ^apps/zerg/backend/
      - id: ruff-format
        files: ^apps/zerg/backend/

  # Local hooks for project-specific checks
  - repo: local
    hooks:
      # TypeScript type checking (catches most AI errors)
      - id: typescript-typecheck
        name: TypeScript type check
        entry: bash -c 'cd apps/zerg/frontend-web && bun run validate:types'
        language: system
        files: ^apps/zerg/frontend-web/.*\.(ts|tsx)$
        pass_filenames: false

      # ESLint + Prettier for frontend
      - id: frontend-lint
        name: Frontend lint + format
        entry: bash -c 'cd apps/zerg/frontend-web && bun run lint --fix'
        language: system
        files: ^apps/zerg/frontend-web/.*\.(ts|tsx|js|jsx)$
        pass_filenames: false

      # WebSocket contract drift guard
      - id: ws-contract-drift
        name: WebSocket contract drift check
        entry: bash -c 'make validate-ws'
        language: system
        files: ^(schemas/ws-protocol.*|apps/zerg/backend/zerg/generated/ws_messages\.py|apps/zerg/frontend-web/src/generated/ws-messages\.ts)$
        pass_filenames: false
```

### Phase 3: Add Python Type Checking (optional, high value)

Add pyright to backend for catching AI type errors:

1. Add to `pyproject.toml`:
   ```toml
   [tool.pyright]
   include = ["zerg"]
   typeCheckingMode = "basic"  # Start permissive
   ```

2. Add pyright hook:
   ```yaml
   - id: python-typecheck
     name: Python type check
     entry: bash -c 'cd apps/zerg/backend && uv run pyright'
     language: system
     files: ^apps/zerg/backend/.*\.py$
     pass_filenames: false
   ```

**Note:** This may surface many existing issues. Start with `basic` mode and tighten later.

---

## Hooks Removed (Dead Code)

| Old Hook | Reason for Removal |
|----------|-------------------|
| `rust-clippy` | Rust frontend removed |
| `dom-id-prefix-check` | Rust frontend removed |
| `contract-field-validation` | Rust frontend removed |
| `pact-contract-coverage` | Rust frontend removed |
| `forbid-direct-node-config-assignment` | Rust frontend removed |
| `prod-config-guard` | Rust frontend removed |
| `stylelint` | Already commented out |
| `asyncapi-validate` | Path fixed, now watches `schemas/` |
| `tool-contract-validation` | Needs path fix or removal |

---

## Migration Path

1. ✅ Spec this document
2. 🔲 Fix ruff format violations (one-time)
3. 🔲 Fix ruff lint violations (one-time)
4. 🔲 Replace pre-commit config
5. 🔲 Test all hooks work: `pre-commit run --all-files`
6. 🔲 Update CODEBASE_SIMPLIFICATION_MASTER.md
7. 🔲 Commit with message: `chore: overhaul pre-commit for AI agent safety`

---

## Escape Hatch

When AI agents (or humans) need to bypass pre-commit:

```bash
git commit --no-verify -m "WIP: skip hooks"
```

CI will still catch issues — this is just for rapid iteration.

---

## Success Criteria

- [ ] `pre-commit run --all-files` passes in < 15s
- [ ] Type errors in Python or TypeScript block commit
- [ ] Format issues auto-fix (don't block)
- [ ] No references to dead paths (`backend/`, `frontend/`, `asyncapi/`)
- [ ] AI agents can commit clean code without manual intervention
