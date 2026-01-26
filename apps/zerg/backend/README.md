# Zerg Backend (FastAPI)

This is the backend service for Swarmlet/Zerg (API, concierge, commis, SSE/WS).

## Start (repo root)

```bash
make dev
```

## Backend deps

```bash
cd apps/zerg/backend
uv sync
```

## Tests

```bash
make test
```

## Docs

- `AGENTS.md` (repo root) — current architecture + commands
- `docs/DEVELOPMENT.md` — local dev guide
