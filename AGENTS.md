# Zerg / Swarmlet

AI agent orchestration platform. Oikos = voice/text UI. Swarmlet = product name.

**Owner**: david010@gmail.com (David Rose)

Skills-Dir: .agents/skills

## Philosophy

**"Trust the AI"** — Modern LLMs are smart enough to figure things out. Give them context and autonomy, not rigid decision trees. No keyword routing, no specialized commiss.

## Quick Reference

**Do not Assume `make dev` is running** during coding sessions.

| URL | What |
|-----|------|
| localhost:30080/dashboard | Main UI |
| localhost:30080/chat | Oikos chat |
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

**Oikos/Commis Flow:**
User message → `OikosService` → `oikos_react_engine` → (spawn_commis) → `AgentInterrupted` → WAITING → commis runs → `commis_resume` → response

**Key Files:**
| File | Purpose |
|------|---------|
| `services/oikos_react_engine.py` | Core ReAct loop |
| `managers/fiche_runner.py` | `run_thread()` entry point |
| `services/commis_runner.py` | Executes commis jobs |
| `tools/builtin/oikos_tools.py` | `spawn_commis`, `get_commis_evidence` |
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
| Oikos Tools | `apps/zerg/backend/docs/supervisor_tools.md` |
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

- (2026-01-22) [gotcha] Runner name+secret auth can collide across owners. If two owners seed runners with same name and secret, the first-created runner wins. Use unique secrets per environment.
- (2026-01-22) [gotcha] Claude Code CLI with z.ai DOES work, but needs: 1) ANTHROPIC_AUTH_TOKEN not ANTHROPIC_API_KEY, 2) unset CLAUDE_CODE_USE_BEDROCK, 3) HOME=/tmp in read-only containers (CLI writes .claude.json config).
- (2026-01-23) [gotcha] Sauron migration is partial: Zerg builtin jobs include backup_sentinel/disk_health/qa/gmail_sync; other scheduled jobs still live in sauron-jobs.
- (2026-01-23) [tool] Codex CLI non-interactive mode: `codex exec -` reads prompt from stdin; `--full-auto` enables automatic execution.
- (2026-01-23) [gotcha] Workspace commiss bypass CommisRunner (no commis_started/tool events); only commis_complete is emitted and diffs live in artifacts, not the oikos summary.
- (2026-01-23) [pattern] Repo tasks should be routed by tool/interface (separate tool or auto-routing); prompt-only enforcement leads to runner_exec misuse.
- (2026-01-24) [gotcha] Tool contracts live in `schemas/tools.yml`; regenerate `apps/zerg/backend/zerg/tools/generated/tool_definitions.py` via `scripts/generate_tool_types.py` instead of editing the generated file.
- (2026-01-24) [gotcha] Oikos tool registration is centralized: add tools in `oikos_tools.py`; `CORE_TOOLS` pulls `SUPERVISOR_TOOL_NAMES`; `oikos_service.py` uses `get_oikos_allowed_tools()`. Tests in `test_core_tools.py` catch drift.
- (2026-01-24) [gotcha] Repo policy: work only on main, no worktrees; confirm `git -C /Users/davidrose/git/zerg status -sb` before changes; no stashing unless explicitly requested.
- (2026-01-24) [tool] Claude Code sessions are stored at `~/.claude/projects/{encoded-cwd}/{sessionId}.jsonl`; `--resume` requires the file locally.
- (2026-01-24) [tool] `CLAUDE_CONFIG_DIR` overrides the entire `~/.claude/` location, enabling shared config/cache paths across machines.
- (2026-01-24) [pattern] Oikos UX: "Human PA" model - kick off tasks, move on, don't block. Commiss report back async. Input should re-enable on `oikos_complete`, not wait for commiss. See `AI-Sessions/2026-01-24-jarvis-worker-ux-design.md`.
- (2026-01-25) [gotcha] `zerg/main.py` load_dotenv(override=True) clobbered E2E env (ENVIRONMENT=test:e2e), preventing CommisJobProcessor startup; use override=False in test/e2e.
- (2026-01-25) [gotcha] Voice TTS playback uses blob URLs; CSP must include `media-src 'self' blob: data:` or audio playback fails in prod.
- (2026-01-25) [gotcha] Telegram channel `webhook_url` only sets the remote webhook; no local webhook handler is wired yet, so inbound delivery still requires polling.
- (2026-01-25) [gotcha] Tests patch `zerg.services.openai_realtime.httpx.AsyncClient`; keep `httpx` imported in the compatibility wrapper after moving realtime helpers.
- (2026-01-25) [pattern] OikosService enforces a single ThreadType.SUPER thread per user ("one brain"); each Oikos message creates an Run tied to that thread.
- (2026-01-25) [gotcha] Voice uploads may send content-type params (e.g., `audio/webm;codecs=opus`); normalize before validation or browser uploads will 400.
- (2026-01-25) [gotcha] Empty or too-short audio yields no transcription; return 422 and show a friendly "try speaking longer" prompt instead of 500.
- (2026-01-26) [gotcha] `spawn_commis` in `oikos_react_engine` parallel path does not raise `FicheInterrupted`, so runs finish SUCCESS and commis results only surface on a later user turn unless WAITING is triggered.
- (2026-01-25) [gotcha] FicheRunner filters out DB-stored system messages; injected `role="system"` thread messages are ignored by LLM context unless you change the filtering.
- (2026-01-25) [gotcha] Legacy continuations may have null `root_run_id`; chain continuations will alias to the wrong run unless you backfill or fall back to `continuation_of_run_id`.
- (2026-01-26) [gotcha] Turn-based voice `/api/oikos/voice/turn` bypasses SSE, so commis/tool UI and streaming events never render; full parity requires routing transcripts through `/api/oikos/chat` (SSE) or emitting equivalent events.
- (2026-01-26) [gotcha] New SSE event types must be added to `EventType` enum or `append_run_event()` won't publish live (modal won't open until reconnect).
- (2026-01-26) [pattern] CI debugging: run commands directly, no `&` background, no `|| echo` swallowing. Let it crash, read the first error. Don't add debug steps before reading failure output.
- (2026-01-27) [gotcha] Life Hub agent log API still uses /ingest/agents/events and /query/agents/sessions; session continuity must target those endpoints.
- (2026-01-27) [gotcha] Sauron /sync reloads manifest but scheduler doesn't reschedule jobs; changes/new jobs won't run until restart or explicit re-schedule.
- (2026-01-27) [gotcha] If Zerg backend has `JOB_QUEUE_ENABLED=1` and `JOBS_GIT_*` set, it will schedule external sauron-jobs too; remove/disable those vars when Sauron is the sole scheduler.
