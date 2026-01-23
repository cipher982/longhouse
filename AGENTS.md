# Zerg / Swarmlet

AI agent orchestration platform. Jarvis = voice/text UI. Swarmlet = product name.

**Owner**: david010@gmail.com (David Rose)

## Philosophy

**"Trust the AI"** — Modern LLMs are smart enough to figure things out. Give them context and autonomy, not rigid decision trees. No keyword routing, no specialized workers. Full spec: `docs/specs/durable-runs-v2.2.md`

## Quick Reference

**Assume `make dev` is running** during coding sessions.

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

## Deployment

**Product**: Swarmlet | **Branch**: `onboard-sauron` | **Server**: zerg (Hetzner)

| Service | URL |
|---------|-----|
| Frontend | https://swarmlet.com |
| API | https://api.swarmlet.com |

```bash
git push origin onboard-sauron    # Triggers auto-deploy
./scripts/smoke-prod.sh           # Validate endpoints
./scripts/get-coolify-logs.sh 1   # Check deploy status
```

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
