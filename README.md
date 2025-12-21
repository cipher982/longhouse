<p align="center">
  <img src="apps/zerg/frontend-web/branding/swarm-logo-master.png" alt="Zerg" width="200" />
</p>

<h1 align="center">Zerg + Jarvis (Unified)</h1>

<p align="center">
  <strong>Supervisor + Workers with a unified single-origin UI.</strong>
</p>

Zerg is the supervisor/worker orchestration backend. Jarvis is the voice/text UI. They now ship behind one nginx entrypoint for same-origin UX.

---

## Current Architecture

```
User → http://localhost:30080 (nginx)
  /            → Unified React SPA (dashboard + /chat)
  /dashboard   → Zerg dashboard (alias)
  /chat        → Jarvis chat UI (SPA route)
  /api/*       → Zerg FastAPI backend (includes Jarvis BFF at /api/jarvis/*)
  /ws/*        → Zerg WS (SSE/WS)

Backend: FastAPI + LangGraph supervisor/worker agents
Workers: disposable agents, artifacts under /data/workers
Frontend: Unified React SPA (Zerg dashboard + Jarvis chat), served same-origin
```

Ports (dev): nginx 30080 external; service ports 47200 (frontend), 47300 (backend).

---

## Highlights

- **Worker supervision (Phases 1–6 complete):** tool events, activity ticker, roundabout monitoring, heuristic/LLM decisions, fail-fast critical errors.
- **Unified frontend (Phases 0–7 complete):** single origin, CORS tightened, cross-nav links, Playwright e2e green.
- **Bun-only JS workspace:** single `bun.lock`; Python via `uv`.
- **Same-origin auth (dev):** `AUTH_DISABLED=1` backend, `VITE_AUTH_ENABLED=false` in `docker/docker-compose.dev.yml`; enable auth in prod.

---

## Commands

- `make dev` – brings up unified stack with nginx front.
- `make zerg` – direct ports stack (backend + frontend).
- Tests: `make test` (unit), `make test-e2e`, `make test-all`, `make test-chat-e2e`.
- Codegen: `make generate-sdk`, `make regen-ws`.

---

## Project Structure

```
apps/
├── zerg/
│   ├── backend/        # FastAPI + LangGraph supervisor/worker
│   ├── frontend-web/   # React dashboard
│   └── e2e/            # Playwright unified tests
└── jarvis/             # Legacy artifacts + shared packages

docker/                 # Compose files + nginx reverse-proxy configs
docs/                   # Specs/PRDs (see below)
scripts/                # Dev tools + generators
```

---

## Docs to know

- `docs/specs/frontend-unification-spec.md` – now marked complete (phases 0–7).
- `docs/specs/worker-supervision-roundabout.md` – phases 1–6 complete, matches code.
- `docs/specs/super-siri-architecture.md` – overall vision.
- `docs/specs/supervisor-ui-spec.md` – pending (future UI work).
- `docs/DEPLOYMENT.md` / `docker/docker-compose.dev.yml` – dev/prod entrypoints (profiles: `full|zerg|prod`).

---

## License

ISC
