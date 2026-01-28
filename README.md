<p align="center">
  <img src="apps/zerg/frontend-web/branding/swarm-logo-master.png" alt="Zerg" width="200" />
</p>

<h1 align="center">Zerg + Oikos (Unified)</h1>

<p align="center">
  <strong>Supervisor + Workers with a unified single-origin UI.</strong>
</p>

Zerg is the supervisor/worker orchestration backend. Oikos is the voice/text UI. They now ship behind one nginx entrypoint for same-origin UX.

---

## Current Architecture

```
User → http://localhost:30080 (nginx)
  /            → Unified React SPA (dashboard + /chat)
  /dashboard   → Zerg dashboard (alias)
  /chat        → Oikos chat UI (SPA route)
  /api/*       → Zerg FastAPI backend (includes Oikos BFF at /api/oikos/*)
  /ws/*        → Zerg WS (SSE/WS)

Backend: FastAPI + LangGraph-free supervisor/worker agents (workflow engine uses LangGraph)
Workers: disposable agents, artifacts under /data/workers
Frontend: Unified React SPA (Zerg dashboard + Oikos chat), served same-origin
```

Ports (dev): nginx 30080 external; service ports 47200 (frontend), 47300 (backend).

---

## Highlights

- **Durable Runs (v2.2):** Runs survive disconnects and timeouts. `asyncio.shield()` prevents server-side cancellation on client timeout; `DEFERRED` state for long-running work.
- **Progressive Disclosure:** Large tool outputs stored by reference with markers; worker evidence fetched on-demand via `get_worker_evidence()`. Context trimming keeps LLM prompts within budget.
- **Worker supervision (v2.1):** tool events, activity ticker, roundabout monitoring (heuristic warnings, no auto-cancel), fail-fast critical errors.
- **Supervisor tool visibility (v2.2):** supervisor-direct tool calls emit SSE tool events and render as inline ToolCards in chat.
- **Jobs Infrastructure:** Scheduled background jobs (backups, monitoring) with manual trigger API.
- **Lazy tool loading:** 65+ tools available via `search_tools()`, ~14 core tools always bound.
- **Unified frontend (v2.1):** single origin, CORS tightened, cross-nav links, Playwright e2e green.
- **Bun-only JS workspace:** single `bun.lock`; Python via `uv`.
- **Same-origin auth (dev):** `AUTH_DISABLED=1` backend, `VITE_AUTH_ENABLED=false` in `docker/docker-compose.dev.yml`; enable auth in prod.

---

## Commands

- `make dev` – brings up unified stack with nginx front.
- Tests: `make test` (unit), `make test-e2e`, `make test-all`, `make test-chat-e2e`, `make test-perf` (latency).
- Prompt Iteration: `cd apps/zerg/backend && uv run python scripts/replay_run.py <run_id>`
- Trace Debugging: `make debug-trace TRACE=<uuid>`
- Video Pipeline: `make video-all` (audio -> record -> process)
- Codegen: `make generate-sdk`, `make regen-ws`, `make regen-sse`.

---

## Health Checks (Production)

- Unified Health: `https://swarmlet.com/health`
- Backend Readiness: `https://swarmlet.com/api/system/health`
- Smoke Test: `./scripts/smoke-prod.sh`

---

## Project Structure

```
apps/
├── zerg/
│   ├── backend/        # FastAPI + supervisor/commis agents
│   ├── frontend-web/   # React dashboard + Oikos chat
│   └── e2e/            # Playwright tests
├── runner/             # Remote execution daemon
└── sauron/             # Standalone scheduler

docker/                 # Compose files + nginx configs
scripts/                # Dev tools + generators
```

---

## Documentation

- `AGENTS.md` – Main project guide (architecture, commands, conventions)
- `apps/zerg/backend/DATABASE.md` – Database patterns
- `apps/zerg/backend/TESTING_STRATEGY.md` – Testing philosophy
- `apps/zerg/backend/evals/README.md` – Eval system
- `apps/sauron/README.md` – Scheduler service

---

## License

ISC
