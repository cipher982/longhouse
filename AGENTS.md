# Zerg / Swarmlet

AI agent orchestration platform. Jarvis = voice/text UI. Swarmlet = product name.

**Owner**: david010@gmail.com (David Rose)

## Philosophy

**"Trust the AI"** — Modern LLMs are smart enough to figure things out. Give them context and autonomy, not rigid decision trees. No keyword routing, no specialized workers. Full spec: `docs/specs/durable-runs-v2.2.md`

## Quick Reference

**Do not Assume `make dev` is running** during coding sessions.

| URL | What |
|-----|------|
| localhost:30080/dashboard | Main UI |
| localhost:30080/chat | Jarvis chat |
| localhost:30080/api/* | Backend API |
| localhost:30080/traces | Debug traces |
| localhost:30080/reliability | System health |

## Essential Commands

```bash
make dev              # Start everything (interactive, tails logs)
make stop             # Stop everything
make test             # Unit tests
make test-e2e-core    # Core E2E (must pass 100%)
make test-e2e         # Full E2E suite
make debug-trace TRACE=<uuid>  # Debug a trace
make regen-ws         # Regenerate WebSocket types
make regen-sse        # Regenerate SSE types
```

## Architecture

```
User → nginx:30080 → FastAPI backend (47300) + React frontend (47200)
```

**Database:** Zerg DB is a schema inside Life Hub's Postgres (same server). No separate sync needed for structured data.

**Supervisor/Worker Flow:**
User message → `SupervisorService` → `supervisor_react_engine` → (spawn_worker) → `AgentInterrupted` → WAITING → worker runs → `worker_resume` → response

**Key Files:**
| File | Purpose |
|------|---------|
| `services/supervisor_react_engine.py` | Core ReAct loop |
| `managers/agent_runner.py` | `run_thread()` entry point |
| `services/worker_runner.py` | Executes worker jobs |
| `tools/builtin/supervisor_tools.py` | `spawn_worker`, `get_worker_evidence` |
| `tools/tool_search.py` | Semantic tool search |

**Lazy Tool Loading:** 65+ tools available, ~12 core tools pre-bound. Others discovered via `search_tools()`.

## Conventions

- **Backend**: FastAPI + Pydantic, `apps/zerg/backend/zerg/`
- **Frontend**: React + React Query, `apps/zerg/frontend-web/`
- **Package managers**: Bun (JS), uv (Python) — never npm/pip
- **Generated code** (don't edit): `src/generated/`, `zerg/generated/`
- **Tests**: Always use `make test*` targets, never direct pytest/playwright

## Gotchas

1. **`make dev` is interactive** — tails logs forever. Use `make dev-bg` for background.
2. **Never use raw `docker compose`** — use Make targets (wrong project names, missing env vars).
3. **Never run tests directly** — `make test` / `make test-e2e` only.
4. **WebSocket/SSE code must sync** — run `make regen-ws` / `make regen-sse` after schema changes.
5. **Auth disabled in dev** — `AUTH_DISABLED=1` set in compose.
6. **Coolify env var changes need redeploy** — restart doesn't pick up new vars.
7. **AGENTS.md is canonical** — `CLAUDE.md` is a symlink, edit AGENTS.md only.

## Pushing Changes

**Prod URLs**: https://swarmlet.com (frontend) | https://api.swarmlet.com (API)

### Before Push
```bash
make test              # Unit tests (required)
make test-e2e-core     # Core E2E - must pass 100%
```

### After Push
Coolify auto-deploys `main`. **Always verify your deploy:**
```bash
make verify-prod       # Full validation: API + browser (~80s)
```
This waits for health, runs API checks (auth, LLM, voice, CRUD), then browser tests.

### If Something Breaks
```bash
./scripts/get-coolify-logs.sh 1   # Check deploy logs
```

### Checklist for Agents
1. ✅ `make test` passes locally
2. ✅ `make test-e2e-core` passes locally
3. ✅ Push to main
4. ✅ Run `make verify-prod` (~80s)
5. ✅ Report result to user

## apps/sauron - Standalone Scheduler

Sauron is the centralized ops scheduler, deployed as a standalone service on clifford VPS. It reuses `zerg.jobs` infrastructure.

**Location:** `apps/sauron/`

**Key files:**
- `main.py` - APScheduler + worker loop
- `api.py` - FastAPI for Jarvis control
- `cli.py` - CLI for manual operations
- `Dockerfile` - Builds from monorepo root
- `docker-compose.yml` - Coolify deployment config

**How it works:**
1. On startup, clones `cipher982/sauron-jobs` repo via `GitSyncService`
2. Loads jobs from `manifest.py` using `zerg.jobs.loader`
3. Schedules jobs with APScheduler
4. Executes via durable queue (same as Zerg backend)

**API endpoints:**
| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/health` | GET | Health check |
| `/status` | GET | Scheduler + git sync status |
| `/jobs` | GET | List all jobs |
| `/jobs/{id}/trigger` | POST | Manual trigger |
| `/sync` | POST | Force git sync |

**Deploy:** Coolify on clifford, separate from main Zerg deployment.

## Deep Dives

| Topic | Guide |
|-------|-------|
| Debugging | `docs/DEBUG.md` |
| Testing | `docs/TESTING.md` |
| E2E Testing | `docs/TESTING_E2E.md` |
| Credentials | `docs/CREDENTIALS.md` |
| Database | `docs/DATABASE.md` |
| Coolify | `docs/COOLIFY_DEBUGGING.md` |
| Architecture spec | `docs/specs/durable-runs-v2.2.md` |
| Jobs infrastructure | `docs/specs/runtime-git-jobs.md` |
| Sauron scheduler | `apps/sauron/README.md` |

## Misc
- GH actions use runners on Cube

---

## Agent Self-Improvement

**Agents: append learnings here.** Human reviews weekly to promote or compact.

### When to Append

- **User corrects you** — Record what you assumed wrong
- **Non-obvious constraint discovered** — Something not in docs that bit you
- **Workaround found** — Errors/failures and what fixed them
- **Pattern that worked well** — Approaches worth repeating

### Format

```
- (YYYY-MM-DD) [category] Specific insight. "X caused Y, fix is Z"
```

Categories: `gotcha`, `pattern`, `tool`, `test`, `deploy`, `perf`

### Rules

1. **Be specific** — Not "had issues with X" but "X fails when Y, use Z instead"
2. **One line per insight** — Keep atomic, date it
3. **Don't delete others' entries** — Human compacts weekly
4. **Propose promotions** — If something keeps being useful, suggest moving to main docs

---

### Learnings

<!-- Agents: append below this line. Human compacts weekly. -->

- (2026-01-21) [meta] Initial self-improvement section setup
- (2026-01-22) [gotcha] Runner name+secret auth can collide across owners. If two owners seed runners with same name and secret, the first-created runner wins. Use unique secrets per environment.
- (2026-01-22) [gotcha] Life Hub triggers hindsight via /api/hindsight/session-ended but Zerg repo currently has no handler; integration gap to resolve.
- (2026-01-22) [pattern] Personal tooling MVP: prioritize delegation flow and async completion over enterprise resiliency concerns.
- (2026-01-22) [pattern] QA agent job at `jobs/qa/`: hybrid determinism pattern - bash collects data, Claude CLI analyzes. State persists in ops.runs.metadata. Always preserve previous state on agent failure.
- (2026-01-22) [gotcha] Claude Code CLI with z.ai DOES work, but needs: 1) ANTHROPIC_AUTH_TOKEN not ANTHROPIC_API_KEY, 2) unset CLAUDE_CODE_USE_BEDROCK, 3) HOME=/tmp in read-only containers (CLI writes .claude.json config).
- (2026-01-23) [gotcha] Cloud worker completion emits/resume happen inside main try; if emit/resume raises, job flips to failed after a successful run. Make SSE/resume best-effort.
- (2026-01-23) [gotcha] Sauron migration is partial: Zerg jobs only include backup_sentinel/disk_health/qa; email-commands + several scheduled jobs still live in sauron.
- (2026-01-23) [tool] `zerg/libs/agent_runner/` superseded by standalone `~/git/hatch/` package. Use `uv tool install -e ~/git/hatch` for global `hatch` CLI. Zerg's copy kept for in-process use but new features go to standalone.
- (2026-01-23) [tool] Codex CLI non-interactive mode: `codex exec -` reads prompt from stdin; `--full-auto` enables automatic execution.
- (2026-01-23) [gotcha] `execution_mode=workspace` runs `hatch` in a git workspace (requires `git_repo`); `standard` runs WorkerRunner in-process. Old names `cloud`/`local` still work for backward compat.
- (2026-01-23) [gotcha] Workspace workers bypass WorkerRunner (no worker_started/tool events); only worker_complete is emitted and diffs live in artifacts, not the supervisor summary.
- (2026-01-23) [pattern] Repo tasks should be routed by tool/interface (separate tool or auto-routing); prompt-only enforcement leads to runner_exec misuse.
- (2026-01-24) [gotcha] Tool contracts live in `schemas/tools.yml`; regenerate `apps/zerg/backend/zerg/tools/generated/tool_definitions.py` via `scripts/generate_tool_types.py` instead of editing the generated file.
- (2026-01-24) [gotcha] Supervisor tool registration is centralized: add tools in `supervisor_tools.py`; `CORE_TOOLS` pulls `SUPERVISOR_TOOL_NAMES`; `supervisor_service.py` uses `get_supervisor_allowed_tools()`. Tests in `test_core_tools.py` catch drift.
- (2026-01-24) [pattern] External jobs loader simplified: `zerg/jobs/loader.py` uses `runpy.run_path()` on `manifest.py` from git repo. Duplicates skip (not fatal), sys.path cleaned after load, git SHA tracked in metadata. Updates require restart.
- (2026-01-24) [pattern] UX needs multi-level alerting: auto-ack obvious “continue?” prompts, hard-stop attention for risky/ambiguous states; keep “fun” vibe without sacrificing triage speed.
- (2026-01-24) [gotcha] Jarvis is no longer a separate app; it’s just the chat page, so unify its styles with the main frontend when refactoring.
- (2026-01-24) [gotcha] Repo policy: work only on main, no worktrees; confirm `git -C /Users/davidrose/git/zerg status -sb` before changes; no stashing unless explicitly requested.
- (2026-01-24) [tool] Claude Code sessions are stored at `~/.claude/projects/{encoded-cwd}/{sessionId}.jsonl`; `--resume` requires the file locally.
- (2026-01-24) [tool] `CLAUDE_CONFIG_DIR` overrides the entire `~/.claude/` location, enabling shared config/cache paths across machines.
- (2026-01-24) [gotcha] `spawn_workspace_worker` is a normal tool (no AgentInterrupted/WAITING), but `worker_spawned` still increments SSE pending_workers, so chat streams can stay open (input disabled) until `worker_complete` even after supervisor_complete.
- (2026-01-24) [pattern] Jarvis UX: "Human PA" model — kick off tasks, move on, don't block. Workers report back async. Input should re-enable on `supervisor_complete`, not wait for workers. See `AI-Sessions/2026-01-24-jarvis-worker-ux-design.md`.
- (2026-01-25) [gotcha] `zerg/main.py` load_dotenv(override=True) clobbered E2E env (ENVIRONMENT=test:e2e), preventing WorkerJobProcessor startup; use override=False in test/e2e.
- (2026-01-25) [gotcha] Voice TTS playback uses blob URLs; CSP must include `media-src 'self' blob: data:` or audio playback fails in prod.
- (2026-01-25) [gotcha] Telegram channel `webhook_url` only sets the remote webhook; no local webhook handler is wired yet, so inbound delivery still requires polling.
- (2026-01-25) [gotcha] Tests patch `zerg.services.openai_realtime.httpx.AsyncClient`; keep `httpx` imported in the compatibility wrapper after moving realtime helpers.
- (2026-01-25) [pattern] SupervisorService enforces a single ThreadType.SUPER thread per user (“one brain”); each Jarvis message creates an AgentRun tied to that thread.
- (2026-01-25) [gotcha] Skills loader/registry must use `skill.name` (SkillEntry has no `.name`); `e.name` raises AttributeError during load/sort.
- (2026-01-25) [gotcha] Frontend CSP `connect-src` must include `api.openai.com` for OpenAI Realtime; otherwise voice connect fails with CSP-blocked fetch.
- (2026-01-25) [gotcha] Worker artifacts (thread.jsonl, tool_calls/*.txt, result.txt) are written after completion; live tail is via `peek_worker_output` and `worker_output_chunk` SSE (buffered from runner exec_chunk).
- (2026-01-25) [gotcha] Voice uploads may send content-type params (e.g., `audio/webm;codecs=opus`); normalize before validation or browser uploads will 400.
- (2026-01-25) [gotcha] Empty or too-short audio yields no transcription; return 422 and show a friendly “try speaking longer” prompt instead of 500.
- (2026-01-25) [gotcha] Client-side min audio size gate prevents tiny blobs from hitting STT and returning empty transcription.
- (2026-01-26) [gotcha] `spawn_worker` in `supervisor_react_engine` parallel path does not raise `AgentInterrupted`, so runs finish SUCCESS and worker results only surface on a later user turn unless WAITING is triggered.
- (2026-01-25) [pattern] Worker inbox continuation: `trigger_worker_inbox_run()` in worker_resume.py handles workers completing after supervisor SUCCESS. Creates continuation run with `RunTrigger.CONTINUATION`, SSE events alias back via `continuation_of_run_id`. Multiple workers: first creates continuation, subsequent merge or chain.
- (2026-01-25) [gotcha] AgentRunner filters out DB-stored system messages; injected `role="system"` thread messages are ignored by LLM context unless you change the filtering.
- (2026-01-25) [gotcha] Legacy continuations may have null `root_run_id`; chain continuations will alias to the wrong run unless you backfill or fall back to `continuation_of_run_id`.
- (2026-01-26) [pattern] When a continuation is already RUNNING, queue worker updates as internal user messages and trigger a follow-up continuation after it completes (don’t inject mid-run).
- (2026-01-26) [pattern] Avoid manual browser-hub testing; use scripted prod E2E with `auth/service-login` + Playwright for real flows.
- (2026-01-26) [test] Prod Playwright reads `SMOKE_TEST_SECRET` from `.env` with quotes; normalize/strip quotes in fixtures to avoid 403 on service-login.
- (2026-01-26) [test] Live voice STT in prod is unreliable with silent WAV; generate audio via `/voice/tts` and transcribe that in live E2E for stability.
- (2026-01-26) [gotcha] Turn-based voice `/api/jarvis/voice/turn` bypasses SSE, so worker/tool UI and streaming events never render; full parity requires routing transcripts through `/api/jarvis/chat` (SSE) or emitting equivalent events.
- (2026-01-26) [gotcha] Resumable SSE closed on `supervisor_complete` with pending workers, dropping worker output/continuations; keep stream open until workers drain (with a short grace window).
- (2026-01-26) [gotcha] Timestamp prefixes on assistant messages leaked into model outputs; only prefix user messages for temporal context.
- (2026-01-26) [gotcha] New SSE event types must be added to `EventType` enum or `append_run_event()` won't publish live (modal won't open until reconnect).
- (2026-01-26) [pattern] CI debugging: run commands directly, no `&` background, no `|| echo` swallowing. Let it crash, read the first error. Don't add debug steps before reading failure output.
- (2026-01-26) [gotcha] Skills platform exists under `apps/zerg/backend/zerg/skills/` but isn't wired into supervisor/worker prompts or tool registry (no SkillIntegration usage yet).
- (2026-01-26) [gotcha] Playwright E2E can time out at webServer startup (0 tests) if the backend fails to boot; ensure repo `.env` (DATABASE_URL) and backend deps are present before running.
- (2026-01-26) [gotcha] Live SSE stream only forwards subscribed events; `show_session_picker` missing in `apps/zerg/backend/zerg/routers/stream.py` means the session picker modal never opens on live Jarvis streams.
- (2026-01-26) [gotcha] CI runner "cube" pods lack a Docker daemon; testcontainers-backed tests fail unless using an external Postgres or non-Docker DB setup.
- (2026-01-27) [pattern] CI backend tests use `--db-mode=external` with per-xdist-worker schemas (`CI_TEST_SCHEMA + _gw0`). SQLAlchemy `schema_translate_map` redirects ORM `zerg.table` references to worker schemas. Dedicated CI Postgres runs in k3s `ci` namespace.
- (2026-01-27) [gotcha] Sauron /sync reloads manifest but scheduler doesn’t reschedule jobs; changes/new jobs won’t run until restart or explicit re-schedule.
- (2026-01-27) [gotcha] ScriptedLLM treats any ToolMessage as a successful workspace worker completion; tool error strings can still yield “Workspace worker completed successfully,” masking spawn failures in E2E.
- (2026-01-27) [gotcha] If Zerg backend has `JOB_QUEUE_ENABLED=1` and `JOBS_GIT_*` set, it will schedule external sauron-jobs too; remove/disable those vars when Sauron is the sole scheduler.
- (2026-01-27) [gotcha] `sauron-jobs` worklog job uses `GITHUB_TOKEN` (GitHub API via gh); don’t remove it from scheduler envs if worklog is enabled.
