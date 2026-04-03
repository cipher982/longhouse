# Runtime Story Simplification

Status: In progress
Spec: `docs/specs/launch-runtime-simplification.md`
Last updated: 2026-04-03

## Goal

Make the launch story match the real product:

- existing sessions become findable immediately
- new Longhouse sessions become controllable after launch
- the session kernel remains the technical identity, not the first emotional hook
- self-hosted is the free primary path and hosted is the convenience deployment of the same loop

## Done when

- README, landing spec, and launch docs all tell the same two-beat story:
  existing sessions become findable first; new Longhouse sessions become controllable second.
- Public copy leads with the outcome (`control sessions after launch`) and uses `session kernel` as the technical identity lower in the page/docs.
- Provider support is documented honestly enough that demos and launch copy do not drift into parity theater.
- Launch-facing copy says some version of `Works on your laptop. Shines on a machine that stays on.`
- Launch-readiness is triaged clearly into must-demo, nice-to-show, and roadmap.
- Install / onboarding work is sequenced behind the story instead of running ahead of it.

## Checklist

- [x] Remove user-facing `commis` / autonomous-agent / server-first wording from prompts and product copy
- [x] Publish one honest provider capability story
- [x] Rename launch-facing cloud work labels to `cloud session` or equivalent
- [x] Remove obviously stale fiche/dashboard-era primary-path language
- [x] Define the deletion path for `OikosService` / `oikos_react_engine` / runner-facing harness seams
- [x] Phase 1a: extract dispatch contract into `services/dispatch_contract.py`
- [x] Phase 1b: commis barrier logic already standalone in `services/commis_barrier.py`
- [x] Refine the launch spec around the two-beat onramp, `ssh+tmux` differentiation, and outcome-first copy
- [x] Align README top matter to the same two-beat story and launch-ready provider truth
- [x] Align the landing-page spec to the same story, capability truth, and launch triage
- [x] Align landing copy and section emphasis to the same story
- [x] Publish the canonical proof-of-value demo journey in launch-facing docs and demo scripts
- [x] Make the hosted boundary explicit and honest until onboarding friction is reduced
- [x] Decide and land the install/onboarding script changes needed for the story:
  import first, start a Longhouse session second, wrappers later
- [x] Spin wrapper mode out as a follow-on opt-in activation slice, not a launch prerequisite
- [x] Remove leftover `managed-local` wording from public docs/UI
- [x] Expose the second activation beat directly inside Timeline with runner-aware `Start Longhouse Session` / runner setup CTAs
- [x] Add launch-critical activation coverage for the three Timeline states:
  no runner, one ready runner, multiple ready runners

## Work Order

### Phase 1: Marketing copy and launch docs

Do now.

- tighten launch spec
- tighten landing spec
- tighten README top matter
- align vocabulary across those three

This is the current highest-leverage work because the product story is drifting faster than the runtime.

### Phase 2: Demo contract and launch truth

Do next.

- freeze one canonical demo journey
- freeze must-demo capability truth
- ensure the landing and README do not promise more than current provider reality

### Phase 3: Install and onboarding activation

Do after the story is stable.

- onboarding should get users to first value fast
- import/ship existing sessions first
- starting a Longhouse session is the second activation beat
- opt-in wrapper mode belongs here, not before copy is stable
- installer, onboarding, and docs should all point at the same activation order

### Phase 4: Public-product cleanup

Do in parallel where easy.

- reduce overexposed Oikos naming in launch-facing surfaces
- keep machine surface visible and legible

### Phase 5: Architectural cleanup

Do separately and do not let it block launch.

- `OikosService` / harness deletion
- deeper provider parity work
- broader session-control polish outside the launch-critical loop

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
- The launch story should optimize for first proof of value, not the hosted signup funnel.
- Story alignment, launch proof, onboarding, and wrapper mode are now the main forward path.
- The first onboarding pass now matches the launch story: import existing sessions first, then start a Longhouse session when the user wants control.
- Wrapper-mode activation shipped as an explicit opt-in flow:
  - installer no longer mutates Claude/Codex launch behavior by default
  - interactive onboarding offers wrapper install explicitly
  - `longhouse wrap --json` gives automation a machine-readable status/results surface
- Runtime/session UI copy now uses `Longhouse session`, `host machine`, and `Live on host` language instead of leaking `managed-local` into user-facing surfaces.
- Session-facing activation and navigation now talk about `machines` instead of `runners`; `runner` stays as the connector/install term inside explicit machine setup and diagnostics.
- Onboarding verification no longer creates a fake visible timeline session; the verification ingest is now hidden as a sidechain session.
- Installer success output and the landing install block now present the same activation order:
  start Longhouse, import existing sessions, then start a Longhouse session when the user wants control.
- Timeline now carries that same second beat in-product:
  users can start a Longhouse session directly when one runner is ready, or jump to runners/setup when it is not.
- The runners grid now participates in that loop too:
  ready machines expose `Start Longhouse Session` directly instead of forcing a runner-detail detour first.
- The empty Timeline CTA is now honest end-to-end:
  `Connect Machine` opens setup immediately, `Start Longhouse Session` opens launch directly, and `Choose Machine` routes to the ready-machine grid.
- Playwright now covers that activation matrix directly, and the runner websocket route honors `commis` routing so runner presence works against the per-worker SQLite E2E harness instead of the default DB.
- Architectural cleanup remains real, but it is not the first thing to do next.
