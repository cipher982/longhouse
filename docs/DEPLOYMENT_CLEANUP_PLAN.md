# Deployment Architecture Cleanup Plan

**Status (December 2025):** Superseded (kept as a short reference)

This file originally tracked cleanup work around legacy Bun workspace Docker hacks (e.g. `packages/design-tokens`) and stale deployment docs. The current deployment path is simpler and does **not** rely on `packages/*` workspaces.

## Current Production Build Layout

- Compose: `docker/docker-compose.prod.yml`
- Backend build: `docker/backend.dockerfile`
- Frontend build: `apps/zerg/frontend-web/Dockerfile` (single-app build)
- Reverse proxy build: `docker/nginx.dockerfile`

## Tokens / Styling (Current)

- Tokens live in `apps/zerg/frontend-web/src/styles/tokens.css` (edited directly; no separate design-tokens package).

## Still Worth Double-Checking

- `.gitignore` does not exclude source under `apps/zerg/frontend-web/src/jarvis/` (avoid broad patterns like `data/`; use `/data/` for repo-root-only).
- Coolify env vars: `VITE_*` are **build-time**; changing them requires a rebuild/deploy (not just a restart).
