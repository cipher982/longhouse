# Zerg Direct Deployment

Production Longhouse on zerg is owned by direct Docker Compose plus the
existing `coolify-proxy` Caddy container. Coolify application records are not
the source of truth for these surfaces.

## Layout

- `/home/zerg/manual-apps/longhouse-demo/`
- `/home/zerg/manual-apps/longhouse-control-plane/`
Public routing is carried by Caddy labels in the compose files. The host still
uses the existing `coolify-proxy` container as the Caddy process, but there are
no Coolify application records for these services.

Each app directory keeps:

- `docker-compose.yml` from this directory, including Caddy labels
- `.env` with compose-only image pins
- `app.env` copied from the old production app environment

Do not commit `app.env` or live `.env` files.

## Deploy Demo Runtime

GitHub Actions calls:

```bash
./scripts/ops/runtime-deploy.sh \
  longhouse-demo \
  --docker-image ghcr.io/cipher982/longhouse-runtime \
  --docker-tag <commit-sha>
```

For local checks:

```bash
RUNTIME_HOST=zerg ./scripts/ops/deploy-status.sh
curl -fsS https://longhouse.ai/api/health
curl -fsS https://control.longhouse.ai/health
```
