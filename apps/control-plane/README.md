# Longhouse Control Plane

Tiny FastAPI service that handles signup, billing, and provisioning of per-user Longhouse instances.

## Dev (local)

```bash
cd apps/control-plane
uv sync
uv run uvicorn control_plane.main:app --reload --port 48080
```

## Environment (minimum)

```
CONTROL_PLANE_ADMIN_TOKEN=...                 # required for admin endpoints
CONTROL_PLANE_JWT_SECRET=...                  # for issuing instance-login tokens
CONTROL_PLANE_DATABASE_URL=postgresql+psycopg://...
CONTROL_PLANE_DOCKER_HOST=unix:///var/run/docker.sock
CONTROL_PLANE_IMAGE=ghcr.io/cipher982/longhouse:latest
CONTROL_PLANE_ROOT_DOMAIN=longhouse.ai
CONTROL_PLANE_PROXY_PROVIDER=caddy            # or traefik
CONTROL_PLANE_PROXY_NETWORK=coolify           # network to attach instances to
CONTROL_PLANE_INSTANCE_JWT_SECRET=...
CONTROL_PLANE_INSTANCE_INTERNAL_API_SECRET=...
CONTROL_PLANE_INSTANCE_FERNET_SECRET=...
CONTROL_PLANE_INSTANCE_TRIGGER_SIGNING_SECRET=...
```

Optional (instance auth):

```
CONTROL_PLANE_INSTANCE_PASSWORD=...
CONTROL_PLANE_INSTANCE_PASSWORD_HASH=...
CONTROL_PLANE_INSTANCE_GOOGLE_CLIENT_ID=...
CONTROL_PLANE_INSTANCE_GOOGLE_CLIENT_SECRET=...
```

## API

See `API.md` for the current endpoints.
