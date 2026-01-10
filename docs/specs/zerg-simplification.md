# Zerg Simplification Spec

**Status:** In Progress
**Created:** 2025-01-10
**Protocol:** SDP-1 (loose)

---

## Executive Summary

Reduce cognitive load and improve testability by:
1. Consolidating scattered test-model gating logic
2. Simplifying over-engineered lazy loading in models_config.py
3. Establishing a sacred "Core Suite" of E2E tests
4. Unifying readiness contracts across pages
5. (Future) Splitting zerg_react_agent.py into focused modules

---

## Decision Log

| # | Decision | Rationale | Revisit If |
|---|----------|-----------|------------|
| 1 | Start with quick wins (phases 1-2) before structural changes | Build confidence, improve test foundation first | Tests still flaky after phase 3 |
| 2 | Remove ZERG_TOOL_STUBS_PATH check for test models | E2E already sets TESTING=1, redundant check | E2E environment changes |
| 3 | Keep models_config.py API surface stable | Avoid breaking consumers during simplification | Need to add new model config features |

---

## Current State

- **E2E Results:** 142 passed, 188 skipped, 1 flaky
- **Test-model gating:** 3 locations with inconsistent logic
- **models_config.py:** 337 LOC for 2.7KB JSON file
- **Readiness contracts:** Inconsistent across pages

---

## Phase 1: Consolidate Test-Model Gating

**Status:** Pending
**Estimated Effort:** Low

### Problem

Test model validation scattered across:
1. `config/models.json` - Lists gpt-mock/gpt-scripted as real models
2. `jarvis_chat.py:161-175` - Bans gpt-mock, allows gpt-scripted if ZERG_TOOL_STUBS_PATH
3. `zerg_react_agent.py:309-311` - Requires TESTING=1 or ZERG_TOOL_STUBS_PATH

Result: API can accept a model that runtime rejects.

### Implementation

1. Create `zerg/testing/test_models.py`:
   ```python
   """Test-only model definitions. Single source of truth."""

   TEST_ONLY_MODELS = frozenset({"gpt-mock", "gpt-scripted"})

   def is_test_model(model_id: str) -> bool:
       """Check if model is test-only."""
       return model_id in TEST_ONLY_MODELS

   def require_testing_mode(model_id: str, settings) -> None:
       """Raise if test model used outside testing mode."""
       if is_test_model(model_id) and not settings.testing:
           raise ValueError(
               f"Test model '{model_id}' requires TESTING=1. "
               "Set environment variable or use a production model."
           )
   ```

2. Update `jarvis_chat.py`:
   - Import from `zerg.testing.test_models`
   - Replace inline checks with `require_testing_mode()`
   - Remove ZERG_TOOL_STUBS_PATH check

3. Update `zerg_react_agent.py`:
   - Import from `zerg.testing.test_models`
   - Replace inline checks with `require_testing_mode()`
   - Remove ZERG_TOOL_STUBS_PATH check

4. Update `config/models.json`:
   - Remove gpt-mock and gpt-scripted entries (they're not real models)

### Acceptance Criteria

- [ ] Single source of truth for test models in `zerg/testing/test_models.py`
- [ ] `jarvis_chat.py` uses consolidated check
- [ ] `zerg_react_agent.py` uses consolidated check
- [ ] No ZERG_TOOL_STUBS_PATH checks remain for model gating
- [ ] `make test` passes
- [ ] `make test-e2e` passes (with TESTING=1)
- [ ] API rejects test models when TESTING!=1

### Test Commands

```bash
make test          # Unit tests pass
make test-e2e      # E2E tests pass (TESTING=1 set by e2e env)
```

---

## Phase 2: Simplify models_config.py

**Status:** Pending
**Estimated Effort:** Low

### Problem

337 LOC with `_LazyTier`, `_LazyMock`, `__getattr__` magic to lazy-load a 2.7KB JSON file.

### Implementation

1. Replace lazy loading with direct load at import:
   ```python
   import json
   from pathlib import Path

   def _load_config():
       path = Path(__file__).parent.parent.parent.parent.parent / "config" / "models.json"
       env_path = os.getenv("MODELS_CONFIG_PATH")
       if env_path:
           path = Path(env_path)
       return json.loads(path.read_text())

   _CONFIG = _load_config()
   ```

2. Replace `_LazyTier` classes with plain strings:
   ```python
   TIER_1 = _CONFIG["text"]["tiers"]["TIER_1"]
   TIER_2 = _CONFIG["text"]["tiers"]["TIER_2"]
   TIER_3 = _CONFIG["text"]["tiers"]["TIER_3"]
   ```

3. Keep public API functions unchanged:
   - `get_model_by_id()`
   - `get_default_model()`
   - `get_all_models()`
   - `get_tier_model()`

4. Delete:
   - `_LazyTier` class
   - `_LazyMock` class
   - `__getattr__` function
   - `_ensure_loaded()` function
   - `_build_models_cache()` function
   - All the `_*` prefixed globals

### Acceptance Criteria

- [ ] models_config.py < 100 LOC
- [ ] No `_Lazy*` classes
- [ ] No `__getattr__` magic
- [ ] All existing public functions work unchanged
- [ ] `make test` passes
- [ ] No import-time errors

### Test Commands

```bash
make test
python -c "from zerg.models_config import TIER_1, get_model_by_id; print(TIER_1, get_model_by_id('gpt-5.2'))"
```

---

## Phase 3: Establish Core Test Suite

**Status:** Pending
**Estimated Effort:** Medium

### Problem

142 passed, 188 skipped, 1 flaky. No distinction between critical and optional tests.

### Implementation

1. Create `apps/zerg/e2e/tests/core/` directory

2. Move/create core tests (~15 tests):
   ```
   core/
     auth.spec.ts           # Login/logout flow
     dashboard-load.spec.ts # Dashboard renders
     agent-crud.spec.ts     # Create/read/update/delete agent
     run-lifecycle.spec.ts  # Start run, see status, complete
     chat-send.spec.ts      # Send message, receive response
     chat-reconnect.spec.ts # Disconnect, reconnect, see history
     canvas-basic.spec.ts   # Open canvas, add node
     core-journey.spec.ts   # (already exists) Supervisor flow
   ```

3. Update `playwright.config.ts`:
   ```typescript
   projects: [
     {
       name: 'core',
       testDir: './tests/core',
       retries: 0,  // No retries for core suite
     },
     {
       name: 'full',
       testDir: './tests',
       retries: 1,
     },
   ]
   ```

4. Add Makefile target:
   ```makefile
   test-e2e-core:
       cd apps/zerg/e2e && PLAYWRIGHT_RETRIES=0 bunx playwright test --project=core
   ```

5. Move flaky/skipped tests to `tests/quarantine/` or fix them

### Acceptance Criteria

- [ ] `tests/core/` directory exists with 10-15 critical tests
- [ ] `make test-e2e-core` runs with 0 retries
- [ ] Core suite is 0 skipped, 0 flaky
- [ ] Quarantine directory for known-flaky tests
- [ ] Documentation updated

### Test Commands

```bash
make test-e2e-core  # Must be 100% green, no retries
make test-e2e       # Full suite (retries allowed)
```

---

## Phase 4: Unify Readiness Contracts

**Status:** Pending
**Estimated Effort:** Medium

### Problem

Different pages use different readiness signals:
- Dashboard/Canvas: `data-ready="true"` = interactive
- Chat: `data-ready="true"` = messages loaded (NOT interactive)
- Chat: `window.__jarvis.ready.chatReady` = actually interactive

### Implementation

1. Define contract in code comment:
   ```typescript
   // Readiness Contract:
   // - data-ready="true"       -> Page is interactive (can click, type)
   // - data-screenshot-ready   -> Content loaded, animations settled (for marketing)
   ```

2. Fix Chat page (`App.tsx`):
   - Only set `data-ready="true"` when `chatReady` is true
   - Add `data-screenshot-ready` for marketing mode

3. Update E2E fixtures to use consistent contract

4. Remove hidden `RunStatusIndicator` approach:
   - Expose run status via existing UI elements with `data-testid`
   - Or add minimal `data-run-status` attribute to chat container

### Acceptance Criteria

- [ ] `data-ready="true"` means interactive on ALL pages
- [ ] Chat page follows same contract as dashboard/canvas
- [ ] E2E tests use consistent readiness checks
- [ ] Marketing screenshots use `data-screenshot-ready`

### Test Commands

```bash
make test-e2e-core
```

---

## Phase 5: Split zerg_react_agent.py (Future)

**Status:** Deferred
**Estimated Effort:** High

### Problem

1268 LOC file does agent loop + telemetry + heartbeats + evidence mounting.

### Implementation (when ready)

```
zerg/agents_def/
  zerg_react_agent.py    # Pure ReAct loop (~200 LOC)

zerg/runtime/
  llm_telemetry.py       # Disk logging, usage tracking
  heartbeat.py           # Heartbeat emission
  tool_execution.py      # Tool call + error detection
```

### Deferral Reason

Higher risk, needs stable test foundation first. Do after phases 1-4 proven.

---

## Progress Tracker

| Phase | Description | Status | Commit | Reviewed |
|-------|-------------|--------|--------|----------|
| 1 | Test-model gating | Pending | - | - |
| 2 | Simplify models_config | Pending | - | - |
| 3 | Core test suite | Pending | - | - |
| 4 | Readiness contracts | Pending | - | - |
| 5 | Split react agent | Deferred | - | - |

---

## Notes for Subtasks

1. **Commit after each meaningful change** with format: `phase N: description`
2. **Update this spec** as you make progress (check boxes, update status)
3. **Do not proceed to next phase** without main thread approval
4. **If blocked**, document blocker and stop

---
