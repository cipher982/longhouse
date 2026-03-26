# Runtime Story Simplification

Status: In progress
Spec: `docs/specs/launch-runtime-simplification.md`
Last updated: 2026-03-25

## Goal

Make the launch story match the real product: sessions, conversations, Oikos, runners, and managed cloud sessions instead of bespoke autonomous agents or fiche-era concepts.

## Done when

- User-facing prompts, tools, and operator pages describe cloud work as managed CLI sessions.
- Provider support is documented honestly for archive, cloud start, continuation, hooks, and telemetry.
- Launch-facing UI/API naming is aligned around `cloud session` and automation-first wording.
- The deletion path for the current Oikos harness is explicit and underway.

## Checklist

- [x] Remove user-facing `commis` / autonomous-agent / server-first wording from prompts and product copy
- [x] Publish one honest provider capability story
- [x] Rename launch-facing cloud work labels to `cloud session` or equivalent
- [x] Remove obviously stale fiche/dashboard-era primary-path language
- [x] Define the deletion path for `OikosService` / `oikos_react_engine` / runner-facing harness seams
- [x] Phase 1a: extract dispatch contract into `services/dispatch_contract.py`

## OikosService / react_engine Deletion Path

### Scope

| Target | LOC | Purpose |
|--------|-----|---------|
| `oikos_service.py` | ~1,816 | Fiche lifecycle, thread management, commis barriers, run orchestration |
| `oikos_react_engine.py` | ~1,182 | ReAct loop, LLM chaining, tool execution, dispatch contract |

41 import sites across routers, services, tests, scripts, and evals.

### Hard Dependencies (must be resolved before deletion)

1. **`RuntimeRunner.run_thread()`** → calls `run_oikos_loop()` — this is the PRIMARY execution path for fiche runs. Cannot delete without a replacement execution engine.
2. **Presence auto-resume** → `invoke_oikos()` in `routers/presence.py` — operator wakeup on permission blocks.
3. **SurfaceOrchestrator** → optional `OikosService` injection in `surfaces/orchestrator.py` — test mocking dependency.
4. **Dispatch contract logic** — `_classify_dispatch_lane()`, `_infer_requested_backend()`, `_apply_dispatch_contract()` are business-critical validation in the react engine.
5. **Commis barrier coordination** — two-phase barrier creation + job state transitions in `oikos_service.py`.

### Recommended Phases

**Phase 1: Extract reusable logic** (non-breaking)
- Extract dispatch contract functions from `oikos_react_engine.py` into a standalone `services/dispatch_contract.py`.
- Extract commis barrier logic into `services/commis_barrier.py`.
- These are business logic that outlives the OikosService class.

**Phase 2: Replace execution path** (requires design)
- Design the replacement for `run_oikos_loop()` — whether that's a simpler function, a different class, or inline in RuntimeRunner.
- The replacement must preserve: iteration guards, tool dispatch, streaming, error recovery, dispatch contract enforcement.

**Phase 3: Remove OikosService class** (after replacement lands)
- Migrate all `OikosService(db)` instantiations to the replacement.
- Delete `oikos_service.py` and `oikos_react_engine.py`.
- Delete orphaned supporting services: `oikos_context.py`, `oikos_commis_context.py`, `oikos_run_lifecycle.py`, `oikos_wakeup_ledger.py`, `oikos_operator_policy.py`, `oikos_shadow_review.py`.
- Delete oikos-specific routers (8 files), tools (3 files), prompts, auth, and events.
- Delete ~18 oikos-specific test files.

**Phase 4: Cleanup**
- Remove oikos entries from `OIKOS_TOOL_NAMES`, tool registration, and job scheduling.
- Update VISION.md and AGENTS.md to reflect the new architecture.

### Not in scope

- The broader Oikos voice/text chat WebSocket (`/ws/oikos`) may survive as a product surface even after the service class is deleted. The deletion target is the execution harness, not the user-facing chat UI.

## Notes

- This task intentionally excludes the browser-vs-machine auth split that was being worked in parallel.
- The remaining work is architectural boundary cleanup, not another copy sweep.
- Phase 1 (extract reusable logic) can be done incrementally without affecting any runtime behavior.
