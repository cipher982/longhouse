# Codebase Simplification Master (Swarmlet / Zerg)

This is the living checklist for cleaning up the repo after multiple direction changes.
It’s intentionally biased toward **high-impact, low-risk** deletions and consolidations first.

Rules for this effort:

- Prefer deleting dead/stale code over “keeping it around just in case”.
- Keep commits small and frequent (audit trail + easy rollback).
- Don’t touch `.env` (sacred).
- Don’t over-engineer: apply the smallest change that removes confusion.

---

## Current Baseline (as of start of this doc)

- Repo structure is primarily under `apps/` (Zerg backend/frontend/e2e, Jarvis web/native, runner).
- There is significant drift in root scripts/configs that still assume old `backend/`, `frontend/`, `asyncapi/` paths.

---

## Phase 0 — Tracking + Guardrails (Start Here)

- [x] Add/maintain this master checklist (this file).
- [ ] Ensure `make validate`/`make test-zerg` do not rely on dead scripts/paths.

---

## Phase 1 — “Stale Paths” Cleanup (Huge ROI, Low Risk)

### 1.1 Root scripts referencing removed directories

- [x] Fix `scripts/fast-contract-check.sh` (currently assumes `frontend/` Rust).
- [ ] Fix or remove `scripts/run_all_tests.sh` (assumes `backend/`, `frontend/`, `e2e/`).
- [ ] Audit and fix any remaining scripts that reference:
  - `backend/` (should be `apps/zerg/backend/`)
  - `frontend/` (should be `apps/zerg/frontend-web/` or `apps/zerg/e2e/`)
  - `asyncapi/` or `asyncapi/chat.yml` (schema is under `schemas/`)

### 1.2 WebSocket generation + drift scripts

- [x] Make `scripts/generate-ws-types-modern.py` output to the _actual_ repo locations:
  - `apps/zerg/backend/zerg/generated/ws_messages.py`
  - `apps/zerg/frontend-web/src/generated/ws-messages.ts`
  - `schemas/ws-protocol.schema.json`
  - `schemas/ws-protocol-v1.json`
- [x] Fix `scripts/regen-ws-code.sh` to use the modern generator + correct schema path.
- [x] Fix `scripts/validate-asyncapi.sh` to validate `schemas/ws-protocol-asyncapi.yml` (not `asyncapi/chat.yml`).
- [x] Fix `scripts/check_ws_drift.sh` to check the correct generated filenames.
- [x] Make WS codegen deterministic so `make validate-ws` stays green (no timestamps / stable newlines).

### 1.3 Legacy trigger scan script

- [x] Fix `scripts/check_legacy_triggers.sh` to scan `apps/zerg/frontend-web/src` (not `frontend/src`).

---

## Phase 2 — Backend Simplification (Medium Risk, High Value)

### 2.1 Remove runtime debug print spam

- [ ] Remove/guard `print(...)` spam in:
  - `apps/zerg/backend/zerg/events/event_bus.py`
  - `apps/zerg/backend/zerg/websocket/manager.py`
  - `apps/zerg/backend/zerg/database.py` (test-only prints should be gated)

### 2.2 WebSocket schema consolidation

- [ ] Migrate remaining usage/tests off `apps/zerg/backend/zerg/schemas/ws_messages.py` → use `apps/zerg/backend/zerg/generated/ws_messages.py`.
- [ ] Delete `apps/zerg/backend/zerg/schemas/ws_messages.py` once unused.
- [ ] Ensure message types used in backend match the generated contracts (avoid “untyped” runtime-only message types).

### 2.3 Tool resolution consolidation

- [ ] Pick a single public tool access API (registry vs resolver) and delete the redundant layers/globals.

### 2.4 Async loop consolidation

- [ ] Route checkpointer async work through `apps/zerg/backend/zerg/utils/async_runner.py` and delete bespoke background-loop code in `apps/zerg/backend/zerg/services/checkpointer.py`.

---

## Phase 3 — Frontend (Zerg Dashboard) Simplification

### 3.1 Single-source config for API/WS base URLs

- [ ] Remove duplicate API base resolution in `apps/zerg/frontend-web/src/services/api.ts` and depend on `apps/zerg/frontend-web/src/lib/config.ts`.

### 3.2 Legacy CSS + legacy selectors removal (later, after tests are stable)

- [ ] Migrate E2E selectors to `data-testid` and delete `apps/zerg/frontend-web/src/styles/legacy.css` + `apps/zerg/frontend-web/src/styles/css/*` incrementally.

---

## Phase 4 — Jarvis Cleanup

### 4.1 Remove tracked Yarn artifacts (Bun-first repo)

- [ ] Delete tracked `apps/jarvis/.yarn/install-state.gz` and stop tracking `.yarn/`.

### 4.2 Remove committed symlink hacks (if feasible)

- [ ] Remove `apps/jarvis/swarm-packages/config` symlink and switch Jarvis to use the root Bun workspace cleanly.

---

## Phase 5 — Repo Hygiene + Deletions

- [ ] Stop tracking/move root-level stale Python project config (`pyproject.toml`) if unused.
- [ ] Remove unused root `tests/` (if not executed by `make test` / CI).
- [ ] Gitignore `scratch/` and move any needed docs into `docs/`.

---

## Phase 6 — CI / Automation Alignment (Optional but Recommended)

- [ ] Update `.github/workflows/*` to match current repo structure + Bun/uv.
- [ ] Update or remove stale `.pre-commit-config*.yaml` (currently references old paths + npm).

---

## Progress Log (append as we go)

Format:

- YYYY-MM-DD: short note — commit `<hash>`

- 2025-12-20: add codebase simplification master checklist — commit `ffc345a`
- 2025-12-20: fix fast contract check script — commit `a783c3d`
- 2025-12-20: canonicalize WebSocket codegen scripts/outputs — commit `36231e3`
- 2025-12-20: make WebSocket codegen deterministic; `make validate-ws` green — commit `d41a443`
