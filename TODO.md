# TODO

Capture list for substantial work. Not quick fixes (do those live).

## For Agents

- Each entry is a self-contained handoff — read it, you have context to start
- Size (1-10) indicates scope: 1 = hour, 5 = day, 10 = week+
- Check off subtasks as you go so next agent knows state
- Add notes under tasks if you hit blockers or learn something

---

## Longhouse Rebrand — Docs + Naming Map (5)

Establish a single public brand (Longhouse) while keeping Oikos as assistant UI and Zerg as internal codename. Docs must align with VISION + OSS onboarding.

**Deliverables:** clear naming map, updated VISION + OSS docs + README, consistent user-facing language.

- [x] Decide and record naming map: **Longhouse** (umbrella), **Oikos** (assistant), **Zerg** (internal codename only)
- [x] Update `VISION.md` to “Longhouse Vision” + add a short naming note (Oikos/Zerg)
- [x] Update `docs/LIGHTWEIGHT-OSS-ONBOARDING.md` to Longhouse naming + CLI examples
- [x] Update `README.md` to Longhouse branding + domain references
- [x] Update `docs/public-launch-task.md` and `docs/oss-onboarding-improvements.md` to reflect Longhouse naming
- [x] Add a short `docs/BRANDING.md` with the naming map + do/don’t usage rules

---

## Longhouse Rebrand — Product/Meta Strings (6)

User-facing strings, metadata, and package descriptions must stop mentioning Swarmlet/Zerg as a brand.

**Files:** `apps/zerg/frontend-web/index.html`, `apps/zerg/frontend-web/public/site.webmanifest`, `package.json`, runner docs, email templates

- [ ] Replace “Swarmlet” with “Longhouse” in frontend HTML metadata + webmanifest
- [ ] Update `package.json` description to Longhouse naming
- [ ] Update runner README/package metadata to Longhouse (e.g., “Longhouse Runner”)
- [ ] Update email templates / notification copy referencing Swarmlet
- [ ] Decide domain swap (`swarmlet.com` → `longhouse.ai`) and update hardcoded URLs if approved

---

## Longhouse Rebrand — CLI / Packages / Images (7)

Package and binary naming so OSS users see Longhouse everywhere.

- [ ] Decide PyPI package name: `longhouse` vs fallback (`longhouse-ai`)
- [ ] Decide CLI binary name: `longhouse` vs fallback (`longhousectl`)
- [ ] Decide npm scope/name for runner: `@longhouse/runner` or `longhouse-runner`
- [ ] Update docker image name for docs/examples (ghcr.io/.../longhouse)
- [ ] Update any installer text / scripts to new names

---

## Frontend Bundling for pip Package (2)

Bundle frontend assets into pip package using importlib.resources. Final polish for SQLite OSS pivot.

**Why:** `pip install zerg && zerg serve` currently works but frontend assets aren't bundled — requires manual setup.

**Files:** `apps/zerg/backend/pyproject.toml`, `apps/zerg/backend/zerg/main.py`

- [ ] Configure hatch to include `frontend-web/dist/` in package
- [ ] Update FastAPI static mount to use `importlib.resources` for packaged assets
- [ ] Test: `pip install zerg` from TestPyPI → UI loads

---

## Prompting Pipeline Hardening (6)

Unify prompt construction across `run_thread`, `run_continuation`, and `run_batch_continuation` to eliminate divergence in tool loading, usage capture, and persistence.

**Why:** Current flows have subtle differences that cause bugs. Memory query behavior varies, tool results can duplicate.

**Files:** `managers/fiche_runner.py`, related service files

- [ ] Create unified prompt/run helper used by all three flows
- [ ] Introduce `PromptContext` dataclass (system + conversation + tool_messages + dynamic_context)
- [ ] Extract single `derive_memory_query(...)` helper for consistent memory behavior
- [ ] Add DB-level idempotency for tool results (unique constraint or `get_or_create`)
- [ ] Split dynamic context into tagged system messages for clearer auditing
- [ ] Add prompt snapshot test fixture for regression checks

---

## Prompt Cache Optimization (5)

**Depends on:** Prompting Pipeline Hardening (unified helper changes message layout)

Reorder message layout to maximize cache hits. Current layout busts cache by injecting dynamic content early.

**Why:** Cache misses = slower + more expensive. Research shows 10-90% cost reduction with proper ordering.

**Current (bad):**
```
[system] → [connector_status] → [memory] → [conversation] → [user_msg]
                ↑ BUST              ↑ BUST
```

**Target:**
```
[system] → [conversation] → [dynamic + user_msg]
 cached      cached           per-turn only
```

**Files:** `managers/fiche_runner.py` (search: `_build_messages` and `_inject_dynamic_context`)

**Principles:**
- Static content at position 0 (tools, system prompt)
- Conversation history next (extends cacheable prefix)
- Dynamic content LAST (connector status, RAG, timestamps)
- Never remove tools — return "disabled" instead

- [ ] Reorder message construction in fiche_runner
- [ ] Verify cache hit rate improves (add logging/metrics)
- [ ] Document the ordering contract

---

## Live Commis Tool Events via Claude Code Hooks (8)

Workspace commis currently emit only `commis_started` and `commis_complete` — no visibility during execution. This task adds **real-time tool event streaming** using Claude Code hooks.

**Why:** UI shows black box during workspace commis (30-60 min). Users can't see what's happening. Live visibility enables monitoring, early cancellation, and debugging.

**Approach:** Claude Code hooks (`PostToolUse`, `PreToolUse`) fire during hatch execution and POST events to Longhouse API. Longhouse emits SSE events to UI in real-time.

**Architecture:**
```
hatch (claude --print)
    └── PostToolUse hook → POST /api/internal/commis/tool_event
                               └── SSE: commis_tool_completed → UI
```

### Phase 1: Hook Infrastructure
**Files:** `config/claude-hooks/`, `scripts/deploy-hooks.sh`

- [ ] Create `config/claude-hooks/settings.json` with PreToolUse + PostToolUse hooks
- [ ] Create `config/claude-hooks/scripts/tool_event.py` — POSTs to Longhouse
- [ ] Create `scripts/deploy-hooks.sh` — deploys to zerg server
- [ ] Test locally: verify hooks fire and POST correctly

### Phase 2: Backend API
**Files:** `routers/oikos_internal.py`, `events/commis_emitter.py`

- [x] Add `POST /api/internal/commis/tool_event` endpoint
- [x] Validate job_id exists and is running
- [x] Auth: internal token (X-Internal-Token)
- [x] Emit SSE events: `commis_tool_started`, `commis_tool_completed`

### Phase 3: Environment Plumbing
**Files:** `services/commis_job_processor.py`, `services/cloud_executor.py`

- [x] Pass env vars to hatch: `LONGHOUSE_CALLBACK_URL`, `COMMIS_JOB_ID`, `COMMIS_CALLBACK_TOKEN`
- [x] Use internal token for auth (`COMMIS_CALLBACK_TOKEN = INTERNAL_API_SECRET`)
- [x] Ensure hooks can reach Longhouse API (loopback default; override via `LONGHOUSE_CALLBACK_URL`)

### Phase 4: Frontend
**Files:** `frontend-web/src/hooks/`, `frontend-web/src/components/`

- [ ] Handle new SSE events in existing listener
- [ ] UI component showing live tool calls during commis
- [ ] Icons/labels per tool type (Edit, Bash, Read, etc.)

### Phase 5: Polish
- [ ] Error handling in hook script (retry, timeout)
- [ ] Rate limiting (debounce rapid tool calls)
- [ ] Optional: persist events for replay after completion
- [ ] Update docs with hook deployment instructions

**Docs:** Claude Code hooks reference: https://docs.anthropic.com/en/docs/claude-code/hooks

**Notes:** Phase 2/3 complete — hook auth uses X-Internal-Token; callback defaults to loopback (`localhost` / `host.docker.internal`) with `LONGHOUSE_CALLBACK_URL` override.

---

## Sauron /sync Reschedule (3)

`/sync` endpoint reloads manifest but APScheduler doesn't reschedule existing jobs. Changed schedules don't take effect until restart.

**Files:** `apps/sauron/sauron/main.py`

- [ ] On sync, diff old vs new jobs
- [ ] Remove jobs no longer in manifest
- [ ] Reschedule jobs with changed cron expressions
- [ ] Add test coverage

---

## Done (Recent)

- [x] **SQLite OSS Pivot** (2026-02-01) — Phases 0-7 complete: DB boot, model compat, agents API, job queue, locks, checkpoints, CLI, onboarding. See `docs/LIGHTWEIGHT-OSS-ONBOARDING.md`
- [x] SWM-1 swarm protocol test (2026-02-01) — Parallel agent coordination validated
- [x] Parallel spawn_commis interrupt fix (2026-01-30) — commit a8264f9d
- [x] Telegram webhook handler (2026-01-30) — commit 2dc1ee0b, `routers/channels_webhooks.py`
- [x] Learnings review compacted 33 → 11 (2026-01-30)
- [x] Sauron gotchas documented (2026-01-30)
- [x] Life Hub agent migration (2026-01-28) — Zerg owns agents DB
- [x] Single-tenant enforcement in agents API (2026-01-29)
