# Zerg Backend (FastAPI)

This is the backend service for Swarmlet/Zerg (API, oikos, commis, SSE/WS).

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
- `VISION.md` (repo root) — product direction and SQLite-first guidance
- `README.md` (repo root) — developer onboarding
