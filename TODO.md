# TODO

Capture list for substantial work. Not quick fixes (do those live).

## For Agents

- Each entry is a self-contained handoff — read it, you have context to start
- Size (1-10) indicates scope: 1 = hour, 5 = day, 10 = week+
- Check off subtasks as you go so next agent knows state
- Add notes under tasks if you hit blockers or learn something

Classification tags (use on section headers): [Launch], [Product], [Infra], [QA/Test], [Docs/Drift], [Tech Debt], [Brand]

---

## Validation Summary (2026-02-11, rev 13)

### Done / Verified
| Section | Status | Notes |
|---------|--------|-------|
| P0 Launch Core | 100% | All 6 items (auth, demo, CTAs, README, FTS5, QA script) |
| Post-GA Follow-ups | 100% | All 5 items |
| OSS Auth | 100% | Password login + rate limiting + hash support |
| FTS5 Search (Phase 1+2) | 100% | Index + triggers + search + snippets + Oikos tools |
| CI Stability (E2E) | ~90% | Dynamic ports, per-run DB, artifacts; schedule gate missing |
| Rebrand (core) | 100% | 13/13 items complete; all user-facing Swarmlet refs removed, OpenAPI regenerated |
| Harness Phase 1 (Commis->Timeline) | 100% | Ingest, environment filter, source badges, regression test |
| Harness Phase 2 (Deprecate Standard) | 100% | Workspace-only default, CommisRunner removed (~2.7K LOC) |
| Harness Phase 3a-3e (Slim Oikos) | 100% | Loop simplified, tools flattened, services decoupled, memory consolidated, skills progressive disclosure |
| Tech Debt IDs 19-43 | 100% | All resolved (removed or relabeled as stable abstractions) |
| Docs/Drift Audit | ~90% | 30+ items fixed; 4 tracked as feature gaps elsewhere |
| Control Plane Token Bug | FIXED | `sub=numeric_user_id` + explicit email claim (commit `d911d500`) |
| Timeline Resume UI | DONE | Resume button on session detail + card hints (commit `2c59a77f`) |
| AGENTS.md Chain | DONE | Global->repo->subdir chain in commis workspaces (commit `81ce535d`) |
| Skill Format Docs | DONE | Migration scripts for Claude Code + Cursor (commit `5cae78af`) |
| Harness Phase 3f-3h | 100% | 3f (MCP server + commis injection + Codex config.toml); 3g (quality gates + hooks + review fixes); 3h (research doc) |
| Shipper Multi-Provider | DONE | Provider abstraction + Claude/Codex/Gemini parsers + review fixes |
| Rust Engine Daemon | DONE | `apps/engine/` — `longhouse-engine connect` replaces Python watcher. 27 MB idle (vs 835 MB), 0% CPU, FSEvents/inotify, zstd compression. Commits `fe52e338`, `ca8fe86f` |
| Engine Hardening | DONE | Rate-limited warn! (error_tracker), log rotation (tracing-appender daily rolling), bounded watcher channel, 32KB raw_line cap, spool backpressure fixed, offline mode (ConnectError→skip batches, health-check 60s), file_state pruning, spool dead-not-delete, 429 jitter+cap. Heartbeat emitter (5min POST /api/agents/heartbeat + local ~/.claude/engine-status.json). Plist hardened (ThrottleInterval/Nice/LowPriorityIO/HardResourceLimits, logs→/dev/null, token removed from args). Server: AgentHeartbeat model + ingest endpoint + stale agent detection job. Codex high-reasoning review found+fixed 2 bugs (ConnectError offline propagation, log pruning filename match). 45+ unit+integration tests. |
| Install/Onboarding | 100% | All items complete: installer, doctor, connect, hooks, MCP, PATH verify, install guide docs (commit 5757d63b) |
| OSS First-Run UX | 100% | Auto-seed on first run + guided empty state + multi-CLI detection + "No CLI" guidance all complete |
| Frontend: Job Secrets UI | 100% | Secrets CRUD + job status + enable/disable + pre-flight UI on `/settings/secrets` |
| Landing Page Redesign | 100% | All phases + meta/OG tags + docs/pricing rewrite + dead CSS removed |
| AGENTS.md Accuracy Audit | DONE | Deploy section, generated paths, gotchas, checklist all verified |
| OSS Packaging Decisions | 100% | Shipper bundled, no built-in HTTPS, auto-token flow, bundle budget — all done |
| Package Metadata Cleanup | DONE | pyproject.toml + backend README product descriptions updated (commit `4bb7f478`) |
| HN Blocker Scan | DONE | No secrets, no stale branding, no broken links in user-facing surfaces |
| Auto-Token Connect Flow | DONE | `longhouse connect` auto-creates device tokens; password login + localhost auto-auth (commits `a7c11f96`, `0435639d`) |
| UI Smoke WS Filter | DONE | WebSocket connection errors excluded from visual smoke test (commit `112a697e`) |
| UI Smoke Baselines | DONE | All 4 smoke tests pass; baselines regenerated for app, public, mobile pages (commits `fdad1dc2`, `2aacf872`) |
| E2E Chat-Send Streaming | DONE | Root cause: APP_PUBLIC_URL leak → WS wrong port. Fixed env + added WS wait guard (commit `61bf95c9`) |
| Agent Infra Consolidation (Phase 4) | 100% | Embeddings + insights + reservations + semantic search + recall + 7 MCP tools + 22 tests. 10 new files, 15 modified. |

### In Progress
| Section | Status | Notes |
|---------|--------|-------|
| Pre-flight Job Validation | 100% | Phase 1-3 done (backend 409 + force param + frontend enable guard + error_type tracking) |
| Semantic Search / Recall UI | 100% | Backend + MCP + frontend toggle all complete |

### Done (move to archive when convenient)
| Section | Status | Notes |
|---------|--------|-------|
| Session Processing (3.5) | 100% | Core module + summarize + briefing + hook + integration tests + consumer migration all done |
| Full Signup Flow | 100% | OAuth + Stripe + webhooks + provisioning + dashboard + landing CTAs + SSO returning users + live E2E test + 53 unit tests. Rate limiting + password reset shipped. |
| Email Infrastructure | DONE | Platform-provided SES email via control plane injection + per-user override via Settings UI + `resolve_email_config()` chain (DB → env fallback). 3 commits: `a6c09f59`, `867b57ac`, `bb36e815` |

### Not Started
| Section | Status | Notes |
|---------|--------|-------|
| Forum Discovery UX | 40% | Presence events + storage + UI done; bucket actions (Park/Snooze/Archive) and extended state model not started |

> Changelogs archived. See git log for session details.

---

## What's Next (Priority Order)

1. ~~**HN Launch Prep**~~ — ✅ Done.
2. ~~**Public Launch Checklist**~~ — ✅ Done.
3. ~~**Full Signup Flow**~~ — ✅ Done (100%). OAuth + Stripe + provisioning + SSO + live E2E + rate limiting + password reset.
4. ~~**Semantic Search / Recall UI**~~ — ✅ Done. Semantic toggle on Timeline, hooks + API functions, Codex-reviewed.
5. ~~**Seed david010 job secrets**~~ — ✅ Done. 4 secrets seeded via PUT API (LLM_BENCH_MONGODB_URI, LIFE_HUB_DB_URL, LIFE_HUB_API_KEY, GITHUB_TOKEN).
6. ~~**Pre-flight Job Validation Phase 3**~~ — ✅ Done. error_type tracking through full pipeline + frontend badges, Codex-reviewed.
7. **Forum Discovery UX** — Presence signals wired (thinking/running/idle), Forum UI shows live state. Remaining: bucket actions (Park/Snooze/Archive), extended state model. [Details](#product-forum-discovery-ux--explicit-presence-signals-7)
8. **Session titles without LLM configured** — biggest UX gap. Without a provider, all sessions say "Claude session". Needs a fallback title strategy (project + branch + first user message, no LLM required). Low effort, high value.
9. **First-session proof point** — After `longhouse connect --install`, user has no confirmation their sessions will actually ship. Add "Waiting for your first real session..." state on Timeline (distinct from demo data). Small effort, fixes the biggest onboarding confusion.
8. **Oikos Dispatch Contract + Compaction** — Deferred; implement when usage demands it. [Details](#product-harness-simplification--commis-to-timeline-8)

---

## [Infra] ✅ Rolling Deploy Hardening + CI E2E (2)

**Goal:** Strengthen rolling deploy safety + cover the deploy flow in CI provisioning E2E.

- [x] Harden rollback path: health check + status updates on failure
- [x] Add CI provisioning E2E deploy step + coverage

## [Product] ✅ Jobs Pack Reliability + Legacy Secrets Bridge (5)

**Goal:** Make external jobs packs load reliably (deps + manifest resilience) and ensure legacy `os.getenv()` jobs see DB-stored secrets safely.

- [x] Install job pack deps at manifest load (single code path, no `--no-deps`)
- [x] Add safe legacy env injection with concurrency guard (registry + queue)
- [x] Add deps/install + env-injection tests in `apps/zerg/backend/tests_lite/`
- [x] Update sauron-jobs: `requirements.txt` + manifest import guards + secret declarations
- [x] Verify: `make test` + `make test-e2e`

## [Infra] ✅ Control Plane OpenAI Proxy Injection (1)

**Goal:** Inject LiteLLM proxy creds into instances and reprovision david010 so jobs can use OPENAI-compatible APIs.

- [x] Add instance OpenAI settings + provisioner env injection + docs
- [x] Set `CONTROL_PLANE_INSTANCE_OPENAI_API_KEY` + `CONTROL_PLANE_INSTANCE_OPENAI_BASE_URL` in Coolify
- [x] Deploy control plane and reprovision david010
- [x] Seed david010 job secrets via API (LLM_BENCH_MONGODB_URI, LIFE_HUB_DB_URL, LIFE_HUB_API_KEY, GITHUB_TOKEN) — seeded 2026-02-18 via PUT /api/jobs/secrets, using clifford Tailscale IP for cross-server DB access

**Notes (2026-02-16):** Instances route through LiteLLM proxy at `llm.drose.io`. All API calls must include `extra_body={"metadata": {"source": "longhouse:component"}}`. gpt-5-nano + text-embedding-3-small added to proxy allowlist. DB-stored LlmProviderConfig rows can silently override env var keys — deleted stale row on david010 to fix 401s.

## [Infra] 🟡 Instance OpenAI Allowlist (1)

**Goal:** Scope LiteLLM proxy routing to personal instances only (avoid global injection).

- [x] Add allowlist setting for instance OpenAI env injection (subdomain/email)
- [x] Update control-plane env + docs to use allowlist (david010 only)

## [Infra] ✅ Runtime Image Dependency Fix (1)

**Goal:** Instance boots cleanly (zstandard missing in runtime image).

- [x] Add `zstandard` to backend deps
- [x] Push + wait for GHCR runtime build
- [x] Reprovision david010 and verify `/api/health`

---

## [Tech Debt] 🗑️ Remove Python Shipper — Migrate to Rust Engine (4)

**Goal:** The Rust engine (`longhouse-engine connect`) replaced the Python watcher daemon. Remove the dead Python shipping/watching code and migrate `longhouse connect --install` to manage the Rust binary instead.

**Context:** Rust engine is DONE and running. Python shipper is still wired into `cli/connect.py` as the backing daemon for `--install`/`--uninstall`/`--status`. Service management (launchd plist, systemd unit) lives in `services/shipper/service.py` and must be migrated to point at the Rust binary before deletion.

**Files to delete** (pure shipping logic, replaced by Rust engine):
- `apps/zerg/backend/zerg/services/shipper/shipper.py` — SessionShipper, SessionWatcher, ShipperConfig, ShipResult
- `apps/zerg/backend/zerg/services/shipper/watcher.py` — file watcher loop
- `apps/zerg/backend/zerg/services/shipper/spool.py` — offline spool (SQLite)
- `apps/zerg/backend/zerg/services/shipper/state.py` — state tracking
- `apps/zerg/backend/zerg/services/shipper/providers/` — provider parsers (keep `parser.py` — still used by commis_job_processor)

**Files to keep / migrate:**
- `hooks.py` — still needed for hook installation in `connect` flow
- `token.py` — token/URL storage used by CLI + MCP server
- `parser.py` — JSONL parsing used by `commis_job_processor.py`
- `service.py` — migrate launchd/systemd logic to manage `longhouse-engine` binary instead of Python process

**CLI changes (`cli/connect.py`):**
- Remove `_ship_file`, `_ship_once`, `_watch_loop`, `_spool_replay_loop`, `_polling_loop` (all replaced by `longhouse-engine connect`)
- Update `--install` to write a plist/unit pointing at `longhouse-engine connect` (reuse migrated service.py logic)
- Remove all `SessionShipper`, `SessionWatcher`, `ShipperConfig`, `ShipResult` imports/usage

**Also fix while here:**
- Remove dead `scan_interval_seconds`, `batch_size`, `max_batch_bytes` from ShipperConfig (unused fields)
- Note: keep zstd decompression in `routers/agents.py` — Rust engine supports zstd as a non-default option (`CompressionAlgo::Zstd`), so this is not dead code

**Subtasks:**
- [x] Write implementation spec for Python shipper removal (apps/zerg/backend/docs/specs/python-shipper-removal.md)
- [x] Migrate `service.py` install/uninstall/status to manage Rust engine binary
- [x] Update `cli/connect.py` `--install` path to use migrated service.py
- [x] Delete shipper.py, watcher.py, spool.py, state.py, providers/
- [x] Update `__init__.py` exports
- [x] Remove dead ShipperConfig fields
- [x] Remove zstd dead code from agents.py ingest
- [x] `make test` + `make test-e2e` pass

---

## [Tech Debt] 🔧 Ingest Pipeline Reliability + Efficiency (3)

**Goal:** Fix reliability and efficiency gaps in the post-ingest LLM/embedding pipeline found in first-principles review.

**Background task reliability (highest priority):**
- FastAPI `BackgroundTasks` are fire-and-forget — lost on process crash, no retry, no persistence (`routers/agents.py:792-798`)
- Replace with a lightweight SQLite-backed task queue (pending/running/done rows) polled by a background worker
- Gives retries, crash recovery, and visibility into failed summaries/embeddings

**Summary is not truly incremental:**
- `_generate_summary_impl` loads *all* events every run (`.all()`), slices in Python (`agents.py:537-607`)
- On CAS conflict it re-reads all events again and may re-run the LLM call
- Fix: store `last_summarized_event_id` on the session; only load events after that cursor

**Embeddings lack per-event cursor:**
- Same full-table scan pattern — session-level `needs_embedding` flag, no per-event cursor (`agents.py:663-677`)
- Fix: per-event `embedded` bool or a high-water mark like summary cursor

**Duplicate title/summary pipelines:**
- `title_generator.py` is a separate OpenAI Responses API flow that bypasses `models_config` and DB fallback (`title_generator.py:94-156`)
- Session summarization already produces a title — consolidate: have `summarize_events()` return a title field, drop `title_generator.py`

**Semaphores are in-process only:**
- `_summary_semaphore` / `_embedding_semaphore` do nothing under multi-worker deploys (`agents.py:412-424`)
- Naturally resolved once we move to a persistent task queue

**Watcher queue coalescing:**
- Rapid file writes enqueue the same path multiple times → redundant parse/ship cycles (`watcher.py:150-152`)
- Dedupe the queue by path before processing (moot after Python shipper removal, skip if doing that first)

**Subtasks:**
- [ ] Design SQLite task queue schema (session_id, task_type, status, attempts, error)
- [ ] Replace BackgroundTasks calls with task queue inserts + polling worker
- [ ] Add `last_summarized_event_id` cursor to sessions table; update summary logic
- [ ] Add per-event embedding cursor or high-water mark
- [ ] Consolidate title generation into summarize_events() output; delete title_generator.py
- [ ] `make test` + `make test-e2e` pass

---

## [Product] 🧠 Harness Simplification & Commis-to-Timeline (8)

**Goal:** Stop building our own agent harness. Lean on CLI agents (Claude Code, Codex, Gemini CLI). Make commis output visible in the timeline. Remove ~25K LOC of dead code.

**Spec:** `apps/zerg/backend/docs/specs/unified-memory-bridge.md` (renamed: Harness Simplification)

### Phases 1-2: Commis->Timeline + Deprecate Standard Mode
> Archived -- 100% complete. Workspace-only mode, timeline ingest, environment filter, source badges, CommisRunner removed (~2.7K LOC). See git history.

### Phase 3: Slim Oikos (5)

**Architecture:** Single toolbox, many agents. All ~60 tools stay as a library. Each agent (Oikos, commis, future) is configured with a subset.

**3a-3e: Archived -- 100% complete.**
> Loop simplified (oikos_react_engine, message_builder, fiche_runner). Tool infra flattened (catalog/unified_access removed, ~1.1K LOC). 9 services decoupled from FicheRunner. Memory consolidated (3 systems -> 2 + KV). Skills progressive disclosure + AGENTS.md chain + skill format docs. See git history for details.

**Remaining 3a items (deferred):**
- [ ] Implement Oikos dispatch contract from spec: direct vs quick-tool vs CLI delegation, with explicit backend intent routing (Claude/Codex/Gemini) and repo-vs-scratch delegation modes
- [ ] Use Claude Compaction API (server-side) or custom summarizer for "infinite thread" context management

Research doc: `docs/specs/3a-deferred-research.md` — both items evaluated and deferred (commit 16c531e6)

**3f: Longhouse MCP Server — expose toolbox to CLI agents (3)**

Industry standard pattern (2025-2026): teams expose internal tooling as MCP servers so CLI agents access shared context mid-task. See VISION.md § "Longhouse MCP Server" for architecture.

- [x] Implement MCP server exposing: `search_sessions`, `get_session_detail`, `memory_read`/`memory_write`, `notify_oikos` (commit `e1207ef2`)
- [x] Support stdio transport (for local hatch subprocesses) and streamable HTTP (for remote/runner agents) (commit `e1207ef2`)
- [x] Auto-register MCP server in Claude Code settings during `longhouse connect --install` (commit `e1207ef2`)
- [x] Auto-configure MCP server for commis spawned via `hatch` (inject into workspace `.claude/settings.json`) (commit `d849ec8d`)
- [x] Add Codex `config.toml` MCP registration path for Codex-backend commis

**3g: Commis quality gates via hooks (2)**

Verification loops (tests/browser checks before commit) boost agent reliability 2-3x (industry consensus 2025-2026). Inject quality gates into commis workspaces.

- [x] Define default commis hook set: `Stop` hook runs `make test` (or configured verify command) before allowing completion
- [x] Inject hooks into commis workspace `.claude/settings.json` at spawn time
- [x] Make verify command configurable per-project (default: `make test` if Makefile exists, else skip)
- [x] Report hook failures back to Oikos via `notify_oikos` MCP tool (when 3f lands)

**3h: Research — Codex App Server protocol + Claude Agent SDK (1)**

Evaluate newer integration paths for tighter commis control vs. current hatch subprocess approach.

- [x] Evaluate Codex App Server (JSON-RPC over stdio) for structured event streaming from Codex-backend commis — Thread/Turn/Item primitives + approval routing
- [x] Evaluate Claude Agent SDK (TypeScript) as alternative to `hatch` subprocess for Claude-backend commis — real-time streaming, programmatic tool injection, better lifecycle control
- [x] Document trade-offs and recommend path forward (subprocess vs SDK vs protocol) — see `docs/specs/3h-research-commis-integration.md`

### Phase 3.5: Session Processing Module + Briefing (5)

**Spec:** `docs/specs/session-processing-module.md`
**Handoff:** `docs/handoffs/2026-02-11-session-processing-discovery.md`

**Goal:** Pre-computed session summaries injected into Claude Code AI context at startup. No other tool does cross-session context injection — differentiating feature.

**Discovery (2026-02-11):** The SessionStart hook (`~/.claude/hooks/longhouse-session-start.sh`) uses `systemMessage` (human-only display). The AI receives nothing. Fix: use `hookSpecificOutput.additionalContext`.

**Phase 1 — Core module + hook fix:**
- [x] Fix SessionStart hook: add `additionalContext` alongside `systemMessage` in `longhouse-session-start.sh` (commit `ac64e0c`)
- [x] Create `zerg/services/session_processing/` module: `content.py`, `tokens.py`, `transcript.py` (commit `355abaab`)
- [x] Golden tests: 112 tests covering noise stripping, redaction, token counting, transcript building (commits `355abaab`, `d6b038fa`)
- [x] Add `summarize.py` with `quick_summary()` + `structured_summary()` + `batch_summarize()` (commit `fb728619`)

**Phase 2 — Briefing pipeline:**
- [x] Add `summary` + `summary_title` columns to `AgentSession` (commit `fb728619`)
- [x] Wire async summary generation into ingest path via `BackgroundTasks` (commit `fb728619`)
- [x] Add `GET /api/agents/briefing?project=X` endpoint with `BriefingResponse` model (commit `fb728619`)
- [x] Update SessionStart hook to call briefing endpoint with fallback to raw sessions list (commit `ac64e0c`)
- [x] Sanitize injected content — safety labels in `format_briefing_context()` (commit `fb728619`)

**Phase 3 — Refactor existing consumers:**
- [x] Migrate `daily_digest.py` to use `session_processing.transcript` + `session_processing.summarize` — replaced inline transcript building, noise stripping, and LLM calls with `build_transcript()` + `quick_summary()`
- [x] Consolidate `safe_parse_json` — exported from `session_processing`, replaced duplicates in `memory_summarizer.py` and `title_generator.py`
- [x] Delete duplicate inline logic — removed `_safe_parse_json` from memory_summarizer + title_generator, removed inline message classes and noise patterns from daily_digest
- [x] Add `POST /api/agents/backfill-summaries` one-shot endpoint + tests to summarize legacy `summary IS NULL` sessions in bounded batches

### Phase 4: Agent Infrastructure Consolidation (8)

**Spec:** `docs/specs/agent-infrastructure-consolidation.md` (v3, Codex-reviewed)
**Goal:** Migrate embeddings, semantic search, recall, insights, and file reservations from Life Hub → Longhouse. Cut over MCP.

**Phase 1 — Foundation (sequential):**
- [x] Fix Gemini `raw_json=""` gap in `shipper/providers/gemini.py`
- [x] Add embedding config to `config/models.json` + `models_config.py` loader
- [x] Add `SessionEmbedding` model + `needs_embedding` column on `AgentSession` + import in `database.py`
- [x] Embedding client (Gemini default, OpenAI alt) + sanitize with `strip_noise`/`redact_secrets`
- [x] Chunking pipeline (`session_processing/embeddings.py`) with event index mapping
- [x] Wire into ingest (BackgroundTask, independent of summary success)
- [x] Backfill endpoint (`POST /api/agents/backfill-embeddings`)

**Phase 1 — Track A (depends on foundation):**
- [x] Embedding cache (in-memory numpy array, lazy-load turn-level)
- [x] Upgrade `search_sessions` MCP tool (semantic mode, hybrid FTS+rerank)
- [x] Add `recall` MCP tool (chunk-level search + event window retrieval)

**Phase 1 — Track B (independent, can parallel):**
- [x] Insights table + `POST /api/insights` + `GET /api/insights` + MCP tools (`log_insight`, `query_insights`)
- [x] File reservations table + API + MCP tools (`reserve_file`, `check_reservation`, `release_reservation`)

**Phase 1 — Tests:**
- [x] `tests_lite/test_embeddings.py`, `test_insights.py`, `test_reservations.py`, `test_semantic_search.py`

### Phase 5: Historical Backfill + Cutover (David-specific) (3)

**Depends on:** Phase 4 complete. **Status: Nearly Complete**

- [x] Backfill ~270 missing sessions from Life Hub — All 230 cursor + 6 swarmlet sessions migrated via direct SQLite INSERT (API rate limits made REST approach impractical). Used `scripts/migrate_sessions_direct.py`.
- [x] Backfill embeddings for all sessions — **RUNNING** in background (~0.9/s, OpenAI text-embedding-3-small, 715/9409 done). Script: `scripts/backfill_embeddings.py` running inside container at `/data/backfill_embeddings.py`.
- [x] Migrate insights history from Life Hub — 241 insights migrated via POST /api/insights
- [x] Dual-test: query both Life Hub and Longhouse, compare results — Longhouse is a superset (10,936 sessions vs Life Hub 4,971). Session counts higher per-day due to sub-agent capture.
- [x] Update `longhouse connect --install` to register expanded MCP tools — already worked, ran successfully
- [x] Update CLAUDE.md global instructions to use Longhouse MCP — updated research tools section + project table
- [x] Remove Life Hub MCP from Claude Code config — removed from `~/.claude.json` and `~/.codex/config.toml`
- [ ] Life Hub agents schema → read-only archive — deferred (smart home + tasks still active on Life Hub)

---

## [Launch] 🎯 HN Launch Priority (This Week)

**Decision:** OSS GA + Hosted Beta in parallel (50/50 positioning). No "OSS-first" bias in copy/CTAs.

### P0 — Launch Core (Required)
> ✅ **Archived** — All 6 P0 items complete (auth, demo, CTAs, README, FTS5, QA). See git history.

### P1 — Hosted Beta (Stretch)
| Priority | Task | Status |
|----------|------|--------|
| 1 | Control Plane Scaffold | ✅ Done |
| 2 | Docker Provisioning | ✅ Done |
| 3 | Google OAuth (control plane) | Not started |
| 4 | Stripe Integration | Not started |
| 5 | Cross-subdomain Auth | Not started |
| 6 | Landing Page Integration | Not started |

**Minimum for launch:** P0 only (self-hosted works end-to-end). Full signup flow tracked in [Full Signup Flow section](#infra-full-signup-flow-8).

---

## [Launch] Post-GA Follow-ups (From 2026-02-03 Swarm)

> ✅ **Archived** — All 5 items complete (rate limiting, hash support, UI fallback, demo-fresh, workflow removal). See git history.

---

## [Infra] ⚠️ Architecture Reality Check (Read First)

**VISION.md describes per-user isolated instances. This is NOW REAL for david010.**

Current reality (as of 2026-02-15):
- **Control plane deployed** at `control.longhouse.ai` (Coolify app `longhouse-control-plane`)
- **david010 instance** is a real isolated container provisioned by the control plane with its own SQLite DB
- **Marketing site** at `longhouse.ai` (Coolify app `longhouse-demo`)
- **Platform-provided email** — Control plane injects SES credentials into instances during provisioning; instances can send email out of the box
- **SSO login** — Control plane issues JWT tokens for cross-subdomain auth
- **Remaining gaps:** Google OAuth credentials not configured on control plane (manual provisioning only), Stripe integration not wired

**Target state:** Full signup flow: Google OAuth → Stripe checkout → auto-provision. See VISION.md for architecture.

See this file for the current launch analysis.

---

## [Launch] 🚨 OSS Auth — Password Login for Self-Hosters (3)

> ✅ **Archived** — Password auth fully implemented. See git history.

---

## [Infra] Domain Split — Marketing vs Personal Instance (4)

> Archived -- DNS, Coolify domains, CORS, AppMode enum all done. Remaining: cross-subdomain OAuth needs control plane (tracked in Control Plane section below).

---

## [Infra] Instance Health Route Returns HTML (1)

> ✅ **Archived** — /api/health returns JSON, route-order fix deployed. See git history.

---

## [Infra] Standardize Health Endpoints (2)

> ✅ **Archived** — Health routes at /api/health + /api/livez, all callers updated. See git history.

---

## [QA/Test] CI Stability — E2E + Smoke (3)

> Archived -- all 5 items complete (dynamic ports, smoke targets, schedule gate, WS test, guardrail script). Note: prod may still return HTTP 525 (Cloudflare origin handshake) -- fix infra routing if needed.

---

## [Product] Landing Page Redesign — Full (6)

**Goal:** Clear user paths, visible CTAs, better contrast. Visitor instantly understands: what it is, who it's for, how to get started.

**⚠️ DEPENDS ON LAUNCH DECISION:**
- **Dual-path (current):** Hosted beta + self-hosted parity in copy and CTAs

Current copy is a mix of both stories. Align to dual-path parity.

**Problems identified (2026-02-05):**
1. ✅ FIXED: Hero CTAs were ghost + not dual-path (both self-host + hosted now visible)
2. Colors too dark — low contrast text, cards blend into background
3. ✅ FIXED: Explicit self-host vs hosted paths (hosted waitlist + self-host install in hero/CTA)
4. ✅ FIXED: Story copy overpromises cross-provider + FTS5 + resume-anywhere
5. ✅ FIXED: CTAs now route to pricing/install (sign-in only when explicitly chosen)

### Phases 1-4: Header, User Paths, Contrast, Hero CTAs
> Archived -- all complete. Sticky header, dual-path CTAs, DeploymentOptions, WCAG contrast fixes, hero restructure. See git history.

**Remaining Phase 2 items:**
- [x] Tertiary link: "Enterprise -->" below hero
- [x] Add comparison table: who runs it, data residency, support, upgrade path

### Phase 5: Story Alignment (2 hours)

Update copy to match VISION.md value prop: Timeline + Search + Resume.

**Hero copy:**
- [x] Headline: "Never lose an AI coding conversation" (or similar)
- [x] Subhead: "Claude Code, Codex, and Gemini sessions in one searchable timeline." (multi-provider now ships — parsers landed 2026-02-10)
- [x] Note: "Local-first. Self-host anytime. Hosted beta waitlist."

**How It Works:**
- [x] Step 1: "Install" → Ships sessions from Claude Code, Codex CLI, and Gemini CLI
- [x] Step 2: "Search" → Keyword search now (FTS5-powered)
- [x] Step 3: "Resume" → Forum resume is Claude-only; Timeline resume planned

**Cut/minimize:**
- [x] IntegrationsSection — kept as "Session Sources" (provider sync status is core story); moved up after HowItWorks
- [x] SkillsSection — collapsed to compact single-line mention; moved below Pricing
- [x] Move Oikos chat to "Features" section, not hero — verified: Oikos is not mentioned anywhere on landing page; hero correctly focuses on Timeline + Search + Resume (commit 98f7a45b)

**Files:** `HeroSection.tsx`, `HowItWorksSection.tsx`, `IntegrationsSection.tsx`, `SkillsSection.tsx`

### Phase 6: Visual Assets (1 hour)

Update screenshots to show Timeline, not old dashboard.

- [x] Update screenshot manifest (`scripts/screenshots.yaml`) for timeline/search/session-detail
- [x] Update landing components to reference new image filenames (`timeline-preview.png`, `session-detail-preview.png`)
- [x] Generate screenshots with dynamic session IDs (commits b208cd61, f8b52e60)
- [x] Add Search as 3rd tab in ProductShowcase (commit e9be2bf5)
- [x] Reorder sections: product demo immediately after hero (commit 21681dd6)
- [x] Remove redundant DeploymentComparison section (commit 9a8cf1cc)
- [x] Fix meta tags/OG for HN sharing — was still "Swarmlet" branding (commit b446744a)
- [x] Remove 3.7MB dead images + 13 dead CSS rules (commits e9be2bf5, 5c637695)
- [ ] Add provider logos inline (Claude, Codex, Cursor, Gemini) — nice-to-have

**Files:** `scripts/screenshots.yaml`, `public/images/landing/`, `HeroSection.tsx`, `ProductShowcase.tsx`

### Checklist (dev-tool landing page best practices 2025-26)

- [x] Above fold: Self-host primary, hosted beta secondary
- [x] Header: Docs + Pricing reachable in 1 click
- [x] CTAs: hero + header + mid-page + footer; labels match next step — normalized (commit 62108045)
- [x] Dark theme: text ≥ 4.5:1, UI components ≥ 3:1 (Phase 3 done; focus indicators still TODO)
- [x] Sticky header doesn't obscure focus / anchors
- [x] Self-host responsibilities spelled out

---

## [Launch] HN Launch Readiness — Remaining (4)

**Goal:** HN reader can install, see value immediately, understand what problem this solves, and start using it.

**Launch Path Decision:** OSS GA + Hosted Beta (optional).

### 🚨 Critical Blockers (Fix First)

- [x] **OSS Auth** — Password login for self-hosters (see dedicated section above)
- [x] **Password-only config bug** — `_validate_required()` now skips Google OAuth validation when password auth is configured
  - File: `apps/zerg/backend/zerg/config/__init__.py:512`
  - Fixed: Skip Google OAuth validation if `LONGHOUSE_PASSWORD` or `LONGHOUSE_PASSWORD_HASH` is set
- [x] **Landing page CTAs** — Copy/flow not dual-path; some CTAs route to sign-in modal instead of install/waitlist

### High Priority

- [x] **Demo mode flag** — `longhouse serve --demo` and `--demo-fresh` implemented
- [x] Installer enforces Python 3.12+ (align with `pyproject.toml`)

### Medium Priority

- [x] **Comparison table** — enhanced for HN launch (commit `f8496f4b`)
  - Shows how Longhouse compares to grep JSONL, Claude Code built-in history, and not tracking

- [x] **Social proof** — Author section in README (commit fd6848cb)
  - ~~Add testimonial or "Built by X" to README~~
  - ~~Show usage stats if you have any early users~~
  - ~~Link to personal Twitter/GitHub for credibility~~

- [ ] **Video walkthrough** (optional, 2 hours)
  - Remotion video studio at `apps/video/` — canonical video production pipeline
  - Landing page wired up — `DEMO_VIDEO_URL` points to `/videos/timeline-demo.mp4` with 404 fallback to placeholder
  - Remaining: run `make video-remotion-web` to render via Remotion, then add to README

---

## [Infra] Full Signup Flow (8)

**What it enables:** User clicks "Get Started" on longhouse.ai → Google OAuth → Stripe checkout → auto-provisioned instance at `{user}.longhouse.ai`

**Architecture:** Tiny FastAPI control plane handles signup/billing/provisioning. Uses Docker API directly (not Coolify). Runtime image bundles frontend + backend per user.

**Current state (2026-02-18):** OAuth, Stripe, webhooks, provisioning trigger, dashboard, provisioning status page, landing page CTAs, and **platform-provided email injection** all implemented. Control plane injects SES env vars into instances during provisioning (`provisioner.py:_env_for()`). OAuth + Stripe creds are set. **Provisioning QA pipeline done** — 53 tests covering full signup flow, Stripe webhooks, provisioning, health checks, and stall idempotency. Fixed `wait_for_health` UnboundLocalError when `publish_ports=True`.

**Decisions / Notes (2026-02-04):**
- Control plane + user instances will live on **zerg** (single host for now).
- Do **not** use Coolify for dynamic provisioning; control plane talks to Docker directly.
- Proxy uses existing Coolify Caddy (caddy-docker-proxy) with caddy labels.
- Wildcard DNS `*.longhouse.ai` ✅ configured (2026-02-04), proxied through Cloudflare.
- Runtime image: `docker/runtime.dockerfile` bundles frontend + backend in single container.

### Phase 0: Routing + DNS Reality Check ⚠️ PARTIAL

- [x] Wildcard DNS `*.longhouse.ai` resolves via Cloudflare (verified 2026-02-05)
- [x] Routing layer: Caddy (coolify-proxy) with caddy-docker-proxy labels — verified working on zerg (2026-02-11). Labels `caddy=david.longhouse.ai` + `caddy.reverse_proxy={{upstreams 8000}}` route traffic correctly.
- [x] Manual provision smoke test: superseded by `scripts/provision-e2e-live.sh` (live E2E against prod)
- [x] Add control-plane → instance auth bridge endpoint — dual-secret validation + email-based user resolution (commits `a2709611`, `d911d500`)

### Phase 1: Scaffold + Auth ✅

- [x] Create `apps/control-plane/` directory structure (FastAPI app, models, routers, services)
- [x] Add provisioner service (Docker API client with Caddy labels)
- [x] Add Instance model with subdomain, container_name, state
- [x] Admin API + minimal HTML UI for manual provisioning
- [x] Add User model with Stripe fields (fields only; no Stripe logic yet)
- [x] Add Google OAuth for control plane login/signup — `GET /auth/google` redirect + `/auth/google/callback` exchange + JWT session cookie + `get_current_user` dependency

### Phase 2: Stripe Integration (3)

**Pre-reqs:** `stripe` Python SDK not in dependencies yet. Add to `apps/control-plane/pyproject.toml`.

**Existing code:** `billing.py` and `webhooks.py` exist as stubs. User model has `stripe_customer_id` and `subscription_status` fields.

- [x] Add `stripe>=11.0` dependency to control plane `pyproject.toml`
- [x] Implement `POST /billing/checkout` — session auth, creates Stripe customer + checkout session
- [x] Implement `POST /webhooks/stripe` — signature verification + 4 event handlers (checkout.session.completed, subscription.updated, subscription.deleted, invoice.payment_failed)
- [x] Webhook: checkout.session.completed triggers auto-provisioning (derives subdomain from email, uniqueness check, calls Provisioner)
- [x] Implement `POST /billing/portal` — Stripe billing portal session
- [x] Add env vars to config + docker-compose: `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID`
- [x] Idempotency: webhook checks subscription_status + existing instance before re-provisioning

### Phase 3: Docker Provisioning ✅ MOSTLY DONE

- [x] Implement Docker API client via local socket
- [x] Provision container with Caddy labels for subdomain routing
- [x] Create SQLite volume per user at `/var/lib/docker/data/longhouse/{subdomain}`
- [x] Implement deprovision (stop + remove container)
- [x] Add health check polling after provision — provisioning status page polls instance health
- [x] Build and push runtime image (`docker/runtime.dockerfile`) to ghcr.io — auto-builds on push via `runtime-image.yml`, publishes to `ghcr.io/cipher982/longhouse-runtime:latest`
- [x] Update CONTROL_PLANE_IMAGE to use runtime image — default now `ghcr.io/cipher982/longhouse-runtime:latest`
- [x] Wire provisioning trigger from Stripe webhook — checkout.session.completed → Provisioner.provision_instance()

### Phase 3.5: Provisioning E2E Gate ✅

- [x] Add CI provisioning script (`scripts/ci/provision-e2e.sh`) with real control-plane + instance smoke checks
- [x] Add GitHub workflow on cube ARC runners (`.github/workflows/provision-e2e.yml`)
- [x] Add CI-only port publishing + writable instance data root for provisioning tests

### Phase 4: Cross-Subdomain Auth (2) — DEFERRED

**Status:** Backend endpoints exist on both sides (CP `login-token` + instance `accept-token`). Dashboard currently links to instance URL directly; user uses instance's own password auth. Full cross-subdomain SSO deferred until needed.

- [x] Control plane `login-token` endpoint exists (issues JWT with `sub=user_id` + `email` claim)
- [x] Instance `accept-token` endpoint exists and handles CP-issued tokens (dual-secret validation)
- [x] Wire auto-redirect after provisioning: provisioning page redirects through `/dashboard/open-instance` for SSO
- [x] Handle returning users: "Sign In" → OAuth → find instance → redirect with token

### Phase 5: Landing Page + Control Plane UI ✅

- [x] Hero CTA: "Get Hosted →" → `control.longhouse.ai/auth/google`
- [x] Pricing CTA: "Get Started" on hosted tier → `control.longhouse.ai/auth/google`
- [x] Control plane home: shows "Sign in with Google" for unauthenticated, redirects to dashboard for authenticated
- [x] Dashboard: shows instance URL/status if provisioned, checkout button if not subscribed, provisioning redirect if in-progress
- [x] Provisioning status page: spinner + health poll + auto-redirect when instance is ready
- [x] Billing portal redirect: dashboard "Manage Billing" → Stripe portal
- [x] Removed WaitlistModal references from hero + pricing sections

### End-to-End User Journey

```
1. User visits longhouse.ai, clicks "Get Started"
2. → Redirects to control.longhouse.ai/auth/google
3. → Google OAuth consent screen
4. → Callback creates/finds User, sets control plane cookie
5. → If no subscription: redirect to /checkout
6. → Stripe Checkout (hosted page)
7. → On success: webhook fires checkout.session.completed
8. → Webhook handler: set subscription active, provision container
9. → User sees provisioning page (polls for health)
10. → Container healthy: redirect to alice.longhouse.ai?token=xxx
11. → Instance validates token, sets session cookie
12. → User lands on their Timeline, ready to connect CLI
```

**Files:** `apps/control-plane/`, `docker/runtime.dockerfile`, landing page components

**Infra status (verified 2026-02-11):**
- ✅ Control plane deployed via Coolify (`longhouse-control-plane`), healthy at `control.longhouse.ai/health`
- ✅ Caddy (coolify-proxy) on zerg handles subdomain routing via caddy-docker-proxy labels
- ✅ Wildcard DNS `*.longhouse.ai` resolves (verified 2026-02-05)
- ⚠️ Docker socket access from control plane container (needs verify)
- ⚠️ Postgres for control plane DB (separate container via docker-compose)
- ✅ Runtime image auto-builds on push via `runtime-image.yml` → `ghcr.io/cipher982/longhouse-runtime:latest`

---

## [Product] Frontend: Job Secrets UI (4)

**Goal:** Settings page where users manage job secrets with rich form fields powered by SecretField metadata. Backend API is complete (`GET /api/jobs/secrets`, `PUT /api/jobs/secrets/{key}`, `DELETE /api/jobs/secrets/{key}`, `GET /api/jobs/{job_id}/secrets/status`). Needs React UI.

**Existing patterns (follow these):**
- **EmailConfigCard** (`apps/zerg/frontend-web/src/components/EmailConfigCard.tsx`) — newest card pattern for Settings page with status display, inline form, test button, save/delete. Follow this for secrets UI.
- **SettingsPage** (`apps/zerg/frontend-web/src/pages/SettingsPage.tsx`) — card-based sections, closest layout pattern
- **SwarmOpsPage** (`SwarmOpsPage.tsx`) — master-detail with list panel + detail panel (use for per-job secret status view)
- **UI components:** `Card`, `Input`, `Button`, `Badge`, `SectionHeader` — custom components, no external UI library
- **API client:** `services/api/` with domain modules (e.g., `services/api/sessions.ts`), uses react-query for cache/fetch
- **Auth:** HttpOnly cookie, no token management needed in frontend

### Phase 1: Secrets Management Page (3)

- [x] Add `services/api/jobSecrets.ts` — API client module (+ types: `JobSecretListItem`, `JobInfo`, etc.)
  - `listJobSecrets()` → `GET /api/jobs/secrets`
  - `upsertJobSecret(key, data)` → `PUT /api/jobs/secrets/{key}`
  - `deleteJobSecret(key)` → `DELETE /api/jobs/secrets/{key}`
  - `getJobSecretsStatus(jobId)` → `GET /api/jobs/{job_id}/secrets/status`
  - `listJobs()` → `GET /api/jobs`
  - `enableJob(jobId, force)` → `POST /api/jobs/{id}/enable`
  - `disableJob(jobId)` → `POST /api/jobs/{id}/disable`
- [x] Add `hooks/useJobSecrets.ts` — React Query hooks for all operations
- [x] Add `pages/JobSecretsPage.tsx` — main secrets management page
  - Card 1: secrets table (key, description, configured badge, updated date, edit/delete)
  - Card 2: job status list with enable/disable toggle + secret indicators
  - Inline add/edit forms, delete confirmation
  - Secret values never displayed — show "configured" badge
- [x] Add route in `App.tsx` — `/settings/secrets`
- [x] Add nav link "Secrets" in `navItems.ts`
- [x] React-query hooks: `useQuery` for list, `useMutation` for upsert/delete with cache invalidation

### Phase 2: Per-Job Secret Status View (2)

- [x] Add per-job secret status panel (implemented inline in `JobSecretsPage.tsx`)
  - Uses `GET /api/jobs/{job_id}/secrets/status` response
  - Renders SecretField metadata: label (fallback to key), required badge
  - Green/yellow status indicator for `configured`
  - "Configure" button next to unconfigured secrets → opens upsert form pre-filled with key
  - Progress indicator: "3 of 5 secrets configured"
- [x] Integrate into job list view on `/settings/secrets` (no separate job detail page yet)
- [ ] Form rendering based on SecretField metadata:
  - `type: "password"` → password input (default)
  - `type: "text"` → text input
  - `type: "url"` → url input with validation
  - `placeholder` → input placeholder
  - `description` → help text below input
  - `required` → asterisk + validation
- [x] Configure flow: user can configure a secret from status view (prefill + scroll to form)

### Phase 3: Polish (1)

- [x] Empty state for no secrets configured — guidance text (docs link optional)
- [x] Toast notifications on save/delete success/failure
- [ ] Keyboard shortcuts: Escape to cancel form, Enter to submit
- [ ] Loading skeletons while fetching
- [ ] Mobile-responsive layout (secrets page should work on tablet)

**Files:** `apps/zerg/frontend-web/src/pages/JobSecretsPage.tsx`, `apps/zerg/frontend-web/src/services/api/jobSecrets.ts`

---

## [Product] Pre-flight Job Validation (3)

**Goal:** Prevent jobs from running when required secrets are missing. Currently, enable/disable is a blind toggle — missing secrets only surface as runtime `RuntimeError` from `require_secret()` in `JobContext`. The status API (`GET /api/jobs/{job_id}/secrets/status`) already returns `configured: bool` per secret; this work wires that data into enforcement points.

**Current behavior:**
- `POST /api/jobs/{id}/enable` → sets `enabled=True`, no checks
- Job runs on schedule → `resolve_secrets()` returns empty string for missing keys → `ctx.require_secret("KEY")` raises `RuntimeError` → job fails with retry
- User sees failure in job history but has no upfront warning

**Target behavior:**
- Enable endpoint checks required secrets are configured → returns 409 with missing list if not
- Frontend shows warning before enable, blocks toggle if secrets missing
- Queue admission optionally skips jobs with missing required secrets (reduces noise)

### Phase 1: Backend Enforcement (2)

- [x] Add `_check_required_secrets(job_id, owner_id, db)` helper in `routers/jobs.py`
  - Calls `_normalize_secret_fields(config.secrets)` to get declared secrets
  - Checks DB + env for each required secret
  - Returns list of missing required secret keys (empty = all good)
- [x] Update `POST /api/jobs/{id}/enable` to call `_check_required_secrets()`
  - If missing secrets: return `409 Conflict` with `{"detail": {"message": "Missing required secrets", "missing": ["KEY1", "KEY2"]}}`
  - If all configured: proceed with enable as before
  - `POST /api/jobs/{id}/disable` does NOT check (always allows disable)
- [x] Add `force` query param: `POST /api/jobs/{id}/enable?force=true` bypasses check
  - For power users / env-var-only setups where secrets aren't in DB
  - Logs warning when force-enabled with missing secrets
- [ ] Optional: queue admission guard in `commis.py` `enqueue_scheduled_run()`
  - Before enqueueing, quick-check if required secrets exist
  - If missing, log warning and skip enqueue (don't crash, don't retry)
  - Configurable: `JOB_PREFLIGHT_ENABLED=true` env var (default true)

### Phase 2: Frontend Guards (1)

- [x] Job list/detail: show warning badge when required secrets are missing
  - `useJobSecretsStatus(jobId)` per job panel, shows `{configured}/{total} secrets` badge
  - Green/yellow dot indicators per secret
- [x] Enable toggle: if secrets missing, show 409-driven warning dialog
  - "This job requires N unconfigured secrets: KEY1, KEY2."
  - Buttons: "Configure Secrets" (scrolls to secrets section) / "Enable Anyway" (force=true) / "Cancel"
- [x] After configuring a secret, auto-refresh job status badges (react-query invalidation on `["job-secrets"]` + `["job-secrets-status"]`)

### Phase 3: Job History Context (1)

- [x] When a job fails due to `RuntimeError` from `require_secret()`, tag the failure
  - `error_type` column on JobRun model, tracked in both direct (registry.py) and queue (commis.py) paths
  - `MissingSecret` type tagged when RuntimeError message matches "not available for job"
  - SQLite migration adds column to existing tables at startup
- [x] Add "last failure reason" summary to job list view — helps users see at a glance which jobs need attention
  - Last Run column shows error_type below status badge (e.g., "Missing secrets")
  - Recent Runs Error column shows error_type as colored badge (warning for MissingSecret, error for others)

**Files:** `apps/zerg/backend/zerg/routers/jobs.py`, `apps/zerg/backend/zerg/jobs/commis.py`, frontend job components

---

## [Launch] Public Launch Checklist (6)

Ensure launch readiness without relying on scattered docs.

- [x] Rewrite README to center Timeline value and 3 install paths (FTS5 + resume/provider copy aligned).
- [x] Add CTA from Chat to "View session trace" after a run.
- [x] Improve Timeline detail header — status badge (completed/in-progress), environment badge, provider dot (commit `8c7db355`)
- [x] Add basic metrics — tool count, turn count, duration shown in header badges (commit `8c7db355`)
- [x] Add event role filters (All/Messages/Tools) in detail view (commit `8c7db355`).
- [x] Search within detail view — event text search added (commit `70acdc73`).
- [x] Core UI smoke snapshots pass (`make qa-ui-smoke`). — Baselines regenerated, all 4 tests pass (commit `fdad1dc2`).
- [x] Shipper smoke test passes — smoke test added (commit 08dbd87b).
- [x] Add packaging smoke test for future install.sh/brew path (if shipped). — packaging smoke test added (commit c27c9aed)

---

## [Launch] HN Post Notes (Condensed)

Keep the HN post short and problem-first. Use install.sh as the canonical path.

- **Title options:** "Show HN: Longhouse – Search your Claude Code sessions" · "Show HN: Never lose a Claude Code conversation again" · "Show HN: Longhouse – A local timeline for AI coding sessions"
- **Angle to emphasize (from industry research):** Context durability is the unsolved problem — benchmarks ignore post-50th-tool-call drift. Longhouse is the session archive that makes agent work durable and searchable. Lean into "your agents do great work, then it vanishes into JSONL" pain point.
- **Comment skeleton:** Problem (JSONL sprawl + context loss) → Solution (timeline + search + resume) → Current state (Claude Code + Codex + Gemini shipping, local-first) → Try it (`curl -fsSL https://get.longhouse.ai/install.sh | bash`, `longhouse serve`)
- **Anticipated Qs:** Why not Claude history? · Cursor support when? · Privacy? · Performance at scale? · How does this compare to just grepping JSONL?
- **Timing:** Tue–Thu mornings PT

---

## [QA/Test] README Test CI (Readme-Contract) (5)

Automate README command verification with explicit, opt-in contracts. Use cube ARC runners where possible.

- [ ] Define `readme-test` JSON block spec (steps, workdir, env, mode, timeout, cleanup).
- [ ] Implement `scripts/run-readme-tests.sh` (extract + run in temp clone, fail fast, save logs).
- [ ] Add `make test-readmes` target (smoke vs full mode flags).
- [ ] Add GitHub Actions workflow using `runs-on: cube` for PR smoke and nightly full.
- [ ] Add `readme-test` blocks to root README + runner/sauron/hatch-agent READMEs.
- [ ] Optional: failure triage via `hatch` agent (summarize logs, suggest fix).

---

## [Product] Forum Discovery UX + Explicit Presence Signals (7)

Make the Forum the canonical discovery UI for sessions, with **explicit** state signals (no heuristics).

**Deliverables:** "Active/Needs You/Parked/Completed/Unknown" are driven by emitted events, not inference.

**Status (2026-02-20):** Core presence infrastructure complete. Hooks emit thinking/running/idle on every response. `session_presence` table stores state. Forum UI shows live glow/pulse. Remaining: extended state model + bucket actions.

- [x] Add ingestion + storage for presence events in the agents schema (SQLite-safe). — `session_presence` table, `POST /api/agents/presence`, upsert per session_id.
- [x] Wire wrappers to emit presence state (Claude first). — `UserPromptSubmit→thinking`, `PreToolUse→running`, `PostToolUse→thinking`, `Stop→idle`.
- [x] Update Forum UI with real state (not heuristics when signals available). — Active rows glow green, inactive fade, canvas entities pulse.
- [ ] Define extended state model: `needs_user`, `blocked`, `parked`, `resumed` — beyond thinking/running/idle.
- [ ] Add user actions in Forum: Park, Snooze, Archive (emit explicit events, change display state).
- [ ] Add a single "Unknown" state in UI for sessions without signals (no pretending).

---

## [Product] Session Titles Without LLM (1)

**Problem:** Without an LLM provider configured, every session is titled "Claude session" / "Codex session" — the timeline is useless as a searchable archive. This is the single biggest UX gap for new self-hosted users.

**Fix:** Generate a fallback title from structured data (no LLM required): `{project} · {branch} · {first 40 chars of first user message}`. Already have all three fields on `AgentSession`. Apply in `getSessionTitle()` in `SessionsPage.tsx` and in the summarization pipeline when LLM is unavailable.

- [ ] Backend: emit a `summary_title` from structured fields when LLM is unavailable (project + branch + first message truncated)
- [ ] Frontend: update `getSessionTitle()` fallback chain to use first user message if no generated title and no project/branch

---

## [Product] First-Session Proof Point (1)

**Problem:** After `longhouse connect --install`, users see demo data and assume setup is done. They have no confirmation their own sessions will actually ship. Many don't discover the hook is broken (wrong PATH, expired token) until they try to search for something days later.

**Fix:** Distinguish demo-only state from "has real sessions" state. Show a visible "Waiting for your first real session..." indicator when the timeline contains ONLY demo-seeded sessions.

- [ ] Backend: add `is_demo` flag to sessions (or detect via source/device_id)
- [ ] Frontend: when all sessions are demo, show persistent "Waiting for your first session — use Claude Code, then come back" banner alongside demo cards

---

## [Product] OSS First-Run UX Polish (5)

Eliminate the "empty timeline" anticlimactic moment and improve discovery for users without Claude Code.

- [x] Seed demo session data on first run (auto-seeds on startup when sessions table is empty; `SKIP_DEMO_SEED=1` to disable)
- [x] Add guided empty state with "Load demo sessions" CTA + connect shipper steps
- [x] Improve "No Claude Code" guidance in onboard wizard (link to alternatives, explain what to do next)
- [x] `longhouse serve --demo` / `--demo-fresh` supported (demo DB)

---

## [Launch] Install + Onboarding Alignment (4)

Close the gap between VISION, README, docs, and the live installer.

- [x] **Canonical install path**: install.sh primary, README aligned, install guide created (commit 5757d63b)
- [x] **Document onboarding wizard**: `docs/install-guide.md` with wizard steps + troubleshooting + manual install (commit 5757d63b)
- [x] **Add `longhouse doctor`** (self-diagnosis for server health, shipper status, config validity); run after install/upgrade and recommend in docs
- [x] **Fix `longhouse connect` default URL** — `connect` + `ship` fallback changed from 47300 to 8080 (commit `426f8c9b`)
- [x] **Installer polish:** verify Claude shim + PATH in a *fresh* shell and print an exact fix line when it fails (commit `1600b094`)
- [x] **Hook-based shipping:** `longhouse ship --file` flag + Stop hook implemented (commit `17a978df`). Hook reads `transcript_path` from stdin JSON, ships single file incrementally. SessionStart hook shows recent sessions on new session start. Remaining: `longhouse connect --install` should auto-inject hooks into `.claude/settings.json`.
- [x] **AGENTS.md chain support:** Support Codex-style AGENTS.md chain (global → repo → subdir) in commis workspaces. Auto-inject Longhouse context (MCP server config, memory pointers) into workspace AGENTS.md when spawning commis.

---

## [Infra] OSS Packaging Decisions (3)

Close the remaining open questions from VISION.md.

- [x] Decide whether the shipper is bundled with the CLI or shipped as a separate package. **Decision: bundled.** Already part of the main package (`pyproject.toml` includes `watchdog`, CLI exposes `connect`/`ship`). Single `pip install longhouse` ships sessions out-of-the-box.
- [x] Decide shipper auth UX for `longhouse connect` (device token flow). **Decision: auto-token.** `longhouse connect` now auto-creates device tokens — tries unauthenticated first (AUTH_DISABLED), falls back to password login (`POST /api/auth/cli-login` → short-lived JWT → create token). Manual `longhouse auth` kept as fallback. Commits `a7c11f96`, `0435639d`.
- [x] Decide HTTPS story for local OSS (`longhouse serve`) — built-in vs reverse proxy guidance. **Decision: no built-in HTTPS.** HTTP on localhost is fine. For remote access, recommend Caddy or nginx reverse proxy (matches Grafana/Jupyter/Datasette pattern).
- [x] Capture current frontend bundle size and set a target budget. (2026-02-11: measured, budget set in VISION.md § "Frontend Bundle Size Baseline")

---

## [Brand] Longhouse Rebrand — Product/Meta Strings (6)

> Archived -- 13/13 items complete. All user-facing Swarmlet refs removed, OpenAPI regenerated, env vars renamed.

- [x] Clean up `experiments/shipper-manual-validation.md` — file already removed; nothing to rebrand

---

## [Brand] Longhouse Rebrand — CLI / Packages / Images (7)

> Archived -- all 4 items complete. npm scope, docker images, installer scripts, runner image all updated.

---

## [Tech Debt] Prompt Cache Optimization (5)

> Archived -- 4/5 items complete. Layout is system->conversation->dynamic, timestamps minute-level, keys sorted, dynamic split.

- [x] Add cache hit logging/metrics

---

## [Product] Session Discovery — FTS5 Search + Oikos Tools (6)

> Phases 1-2 archived -- FTS5 search bar + 4 Oikos session tools all done. Remaining: embeddings.

### Phase 3: Embeddings for Oikos

> **Superseded by Agent Infrastructure Consolidation (Phase 4 in Harness section).** Spec: `docs/specs/agent-infrastructure-consolidation.md`. Embeddings, semantic search, recall, insights, and file reservations are all covered there.

---

## [QA/Test] UI QA Screenshot Capture System (4)

**Goal:** Flexible, low-friction screenshot capture for agents + humans; clear instructions; minimal token cost.

- [ ] Inventory current screenshot/Playwright flows and pain points
- [ ] Prototype a simple capture CLI/API (local dev + headless) and document usage
- [ ] Add agent-friendly capture path (MCP/tool or skill) with stable output paths
- [ ] Add docs + examples; ensure instructions are short and reproducible
- [x] Fix ui-capture a11y snapshot: Playwright 1.57 has no `page.accessibility`; use `locator.ariaSnapshot()` or guard missing API and still write trace/manifest on partial failure
- [ ] Add SCENE=empty reset endpoint (or CLI) to clear sessions; update docs to note current no-op until available

---

## [Docs/Drift] Findings / Drift Audit (2026-02-05)

> 30+ items fixed as of 2026-02-10. Struck-through items archived -- see git history. Open items below.

**Open drift items:**
- [Infra/docs] DB size claim stale; prod DB reset 2026-02-05 (no users). Update docs/launch notes once data exists.
- [Docs vs release] PyPI version likely lags repo; verify `longhouse` version on PyPI before making release claims.
- ~~[Docs vs UI] Timeline resume only in Forum Drop-In (Claude-only), not on `/timeline`.~~ **FIXED** — Resume button works on SessionDetailPage for Claude sessions.
- ~~[Docs vs code] Installer lacks PATH-based Claude shim + fresh-shell verification.~~ **FIXED** — fresh-shell PATH verification added (commit `1600b094`).
- ~~[Docs vs UI] Timeline empty state has no "Load demo" CTA.~~ **FIXED** — guided empty state with "Load demo sessions" button + connect steps.

---

## [Tech Debt] Evidence-Backed Refactor Ideas (Ranked)

> IDs 19-43 resolved (2026-02-10). Postgres cleanup archived (2026-02-05). Evidence scripts in `ideas/evidence/`. Three items relabeled as stable abstractions (not dead code):

- [ID 28] Relabel feedback system compat methods -- actively called, not dead code. Evidence: `ideas/evidence/53_evidence_feedback_system_compat.sh`
- [ID 41] Relabel legacy modal pattern CSS -- actively used by 8+ components; refactor later. Evidence: `ideas/evidence/48_evidence_modal_css_legacy.sh`
- [ID 43] Relabel legacy token aliases -- 95+ active CSS refs; stable abstraction. Schedule with broader CSS refactor. Evidence: `ideas/evidence/50_evidence_tokens_css_legacy_aliases.sh`

---

## [QA/Test] QA Plan (Virtual QA Team) (2026-02-02)

(Former QA_PLAN.md. Keep this plan updated here only.)

Date: 2026-02-02
Owner: Longhouse (Zerg) core
Scope: SQLite-only, timeline-first product with dual-path positioning (self-host + hosted)

### Goals (Vision-Aligned)
- Zero-friction OSS onboarding (install + onboard + demo) works on first run.
- Timeline/demo data feels alive immediately (no API keys required).
- Session ingest is reliable and lossless (shipper -> ingest -> timeline).
- Background agents (commis/runners) are stable and debuggable.
- No waiting for bug reports: automated QA catches regressions before users do.

### Current QA Inventory (What We Already Have)
- Makefile test tiers: `make test` (SQLite-lite), `make test-legacy`, `make test-e2e` (core + a11y), `make test-zerg-e2e`, `make test-frontend-unit`, `make test-hatch-agent`, `make test-runner-unit`, `make test-shipper-e2e`, `make onboarding-sqlite`, `make onboarding-funnel`, `make qa-oss`.
- Playwright E2E with core suite + a11y, visual baselines, perf tests (some skipped).
- Backend pytest suites: unit + integration; SQLite-lite tests in `tests_lite/`.
- Docs-as-source onboarding contract + Playwright test for README contract.
- Shipper tests (unit + integration), runner unit tests.

### Gaps vs Vision (What’s Missing / Fragile)
1) Docs/landing copy still overpromise features (cross-device resume, multi-provider); no automated drift checks in CI.
2) Installer + CLI onboarding flows lack robust automated tests across OS targets.
3) Demo DB pipeline is new; no automated validation that demo DB builds and UI uses it.
4) E2E commis/session-continuity failures (timeouts) -> core suite stability risk.
5) Remaining E2E skips are perf/visual suites only; dev-only/event-bus + unimplemented-feature specs removed and tracked in TODO instead.
6) Shipper end-to-end is opt-in and skipped by default; no required CI gate.
7) Runner and commis execution lack full integration tests with real WebSocket channel.
8) Real-time events (SSE/WS) core coverage is enabled; advanced WS protocol/queue tests are deferred until backend ack support exists.
9) No formal OS matrix for OSS install (macOS/Linux/WSL).
10) OSS user QA script exists (`scripts/qa-oss.sh`), but CI wiring is still pending.
11) ✅ FIXED: Timeline search E2E is now part of `test-e2e-core` gating.
12) ✅ FIXED: Oikos session discovery tools now have unit coverage.
13) ✅ FIXED: FTS trigger integrity tests cover update/delete index consistency.
14) Scheduling/trigger management UI remains unimplemented; E2E specs removed until product work lands.

### Virtual QA Team (Agent Roles)
Use commis/runners + hatch agents to form a lightweight QA org that runs locally or in CI.

- QA Lead (Coordinator): owns test matrix + gating; assigns tasks to agents.
- Spec Guardian: parses VISION/README, flags drift, updates onboarding contract tests.
- Installer Guardian: validates `install.sh` and CLI `longhouse onboard` flows on macOS + Linux.
- Shipper Guardian: validates JSONL -> ingest -> timeline continuity.
- Commis/Runner Guardian: validates background jobs and runner_exec end-to-end.
- E2E Explorer: maintains Playwright core suite + a11y + visual baselines.
- Fuzzer: property-based + fuzz tests for APIs, websocket envelopes, ingest parser.
- Perf/UX Agent: enforces latency budgets and visual baseline stability.

### QA System Architecture (How It Runs)

#### 1) QA Matrix (what must be tested)

User Paths
- OSS local: install -> onboard -> demo -> timeline -> ingest -> search
- Hosted: signup -> instance -> timeline -> ingest -> session query
- Power user: runner -> exec -> commis -> session continuity

System Layers
- Unit (fast, deterministic)
- Integration (real DB, real services, mocked external LLMs)
- E2E (UI + API)
- Contract/Docs-as-Source
- Perf + Visual + A11y
- Security + Dependency hygiene

Data States
- Empty DB
- Demo DB (seeded SQLite)
- Real ingest from JSONL

Providers
- Claude Code, Codex, Gemini (parsers shipped 2026-02-10), Cursor (schema + ingest tests only)

#### 2) Tiered Test Gates

Tier 0 (local fast)
- lint-test-patterns, type checks, OpenAPI contract validation
- `make test` (SQLite-lite backend)
- `make test-frontend-unit`

Tier 1 (OSS path gate)
- `make onboarding-sqlite`
- Build demo DB + verify demo UI loads sessions
- CLI smoke: `longhouse onboard --quick --no-shipper` (headless)

Tier 2 (Core UX gate)
- `make test-e2e-core` (Playwright core, no skips)
- `make test-e2e-a11y`

Tier 3 (System gate)
- Shipper E2E with local backend (no skip)
- Runner + commis integration (websocket + task execution)

Tier 4 (Nightly)
- Full E2E suite, visual baselines, performance tests
- Optional live evals (requires API keys; runs on schedule)

#### 3) OSS QA Script (User-Run)

New script target: `scripts/qa-oss.sh` (or `longhouse doctor --full`).
Purpose: emulate the exact OSS user journey and catch regressions early.

Suggested flow:
1. Environment checks (Python/uv/bun, sqlite version)
2. Build demo DB (`demo-db`) and validate schema
3. Run `make onboarding-sqlite`
4. Boot demo stack (short-lived) and verify:
   - /api/health
   - /api/agents/sessions
   - demo timeline displays sessions
5. Run `make test` + `make test-frontend-unit`
6. Run `make test-e2e-core` (optional flag for CI vs local)
7. Print a short “OK / FAIL” summary

#### 4) LLM/Agent-Driven QA

- Test Synthesizer: generate Playwright tests from “journey specs” (YAML) and Vision changes.
- Failure Triage: summarize Playwright/pytest failures into reproducible steps + suspect areas.
- Regression Miner: when a bug is fixed, auto-suggest a new test case in the same area.
- Drift Checker: diff VISION/README to current UI selectors (CTA drift).

#### 5) Flake/Skip Elimination Strategy

- Replace “skipped until LLM mocking” with deterministic mock server.
- Convert flaky tests to stable selectors or API-assisted setup.
- Establish “no skip in core suite” rule; allow skips only in nightly/optional suites.

### Priority Backlog (Execution Plan)

P0 (now)
- Align README onboarding-contract with SQLite-first path.
- Add installer/CLI tests (install.sh, longhouse onboard, longhouse serve).
- Make demo DB build + demo load test part of OSS gate.
- Fix commis/session-continuity E2E timeouts (core suite must be 100% pass).
- Stabilize /api/health checks in tests (already in onboarding-sqlite).

P1 (next)
- Shipper E2E run in CI with a local backend (no skip).
- Runner + commis integration E2E (spawn runner, execute, verify run log).
- Unskip websocket/SSE tests by adding deterministic harness.
- Add LLM mock server for streaming tests (unskip chat_streaming, token tests).

P2 (after)
- Performance budgets (chat latency, timeline load) + baseline alerts.
- Visual baselines for landing + timeline + forum.
- Security/dependency scanning (npm audit + pip/uv audit).
- OS matrix for installer (macOS + Linux + WSL).

### Reporting & Artifacts
- Always collect Playwright traces and screenshots on failure.
- Export concise summaries: failed test, repro steps, suspected area.
- Store “last-known-good” test results and compare on regressions.

### Ownership & Cadence
- Per-PR: Tier 0 + Tier 1 + Tier 2 (core must pass).
- Nightly: Tier 3 + Tier 4.
- Release: all tiers + live evals (if keys available).

### Immediate Next Steps
1. Update onboarding contract to match SQLite-only path (no Docker). ✅
2. Add OSS QA script (new target) and wire to CI. ✅ (CI wiring pending)
3. Fix commis/session-continuity E2E failures and remove skip if possible.
4. Introduce deterministic LLM mock server so streaming tests can run.
5. Add demo DB validation to onboarding and E2E flows.
