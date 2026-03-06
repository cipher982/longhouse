---
name: zerg-ship
description: Zerg/Longhouse full ship cycle — test, deploy, QA, verify. Use when pushing changes to production or doing a full dev→deploy iteration.
---

# Zerg Ship Cycle

## The Loop

```
make test            # unit tests (~9s) — fix failures before proceeding, never push a failing suite
git push origin main # triggers GHCR build if backend/frontend/dockerfile changed
gh run watch <id>    # wait for runtime image (see below)
~/git/me/mytech/scripts/coolify-deploy.sh longhouse-demo
~/git/me/mytech/scripts/coolify-deploy.sh longhouse-control-plane
# reprovision user instances (see below)
make qa-live         # 5 Playwright tests against live instance (~5s)
```

## QA Harness

```bash
make qa-live                          # default: david010.longhouse.ai
QA_INSTANCE_URL=https://other.longhouse.ai make qa-live  # other instance
```

Tests: auth + timeline, forum redirect, session detail, health, agents API, AI search toggle, recall panel, and briefings page.
Exit 0 = pass. Fail screenshots → `/tmp/qa-live-fail-{test}.png`.

Auth: `qa-live.sh` now delegates to `run-prod-e2e.sh`, resolves the hosted instance by subdomain, and mints a fresh hosted login token through the control plane.
API calls still use `~/.claude/longhouse-device-token` (`X-Agents-Token` header) for `/api/agents/*`; browser pages use the hosted session cookie exchanged from `SMOKE_LOGIN_TOKEN`.

**Two auth systems — don't mix them:**
- Browser pages: hosted login-token → `longhouse_session` cookie
- `/api/agents/*` endpoints: device token → `X-Agents-Token` header

## Reprovision User Instance

```bash
# Find control plane container by service label (hash changes on every deploy)
CONTAINER=$(ssh zerg "docker ps --filter label=coolify.serviceName=longhouse-control-plane --format '{{.Names}}' | head -1")
ADMIN_TOKEN=$(ssh zerg "docker exec $CONTAINER python -c 'from control_plane.config import settings; print(settings.admin_token)'")

# List instances to get the right ID (don't hardcode — use subdomain to find it)
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" https://control.longhouse.ai/api/instances \
  | python3 -c "import sys,json; [print(i['id'], i['subdomain']) for i in json.load(sys.stdin)['instances']]"

# Reprovision (stops+removes+recreates with latest image — data is safe, SQLite bind-mounted)
curl -s -X POST -H "X-Admin-Token: $ADMIN_TOKEN" https://control.longhouse.ai/api/instances/<id>/reprovision

# Verify health
sleep 15 && curl -s https://david010.longhouse.ai/api/health | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])"  # healthy
```

Data survives reprovision — SQLite at `/var/app-data/longhouse/<subdomain>/longhouse.db` (host bind mount).

## Wait for GHCR Build

```bash
gh run watch $(gh run list --workflow runtime-image.yml --limit 1 --json databaseId -q '.[0].databaseId') --exit-status
```

Path filters: build only triggers if `apps/zerg/backend/`, `apps/zerg/frontend-web/`, or `docker/` changed. Docs-only pushes skip it.

## Logs When Things Break

```bash
# User instance
ssh zerg 'docker logs longhouse-david010 --tail 50'

# Marketing site / control plane
coolify app logs longhouse-demo
coolify app logs longhouse-control-plane

# Engine daemon (on dev machine)
tail -f ~/.claude/logs/engine.log.$(date +%Y-%m-%d)
```

## Definition of Done

- [ ] `make test` passes (no failures)
- [ ] `make qa-live` 5/5 passed
- [ ] `gh run watch` GHCR build success
- [ ] `curl -s https://david010.longhouse.ai/api/health | python3 -c "import sys,json; print(json.load(sys.stdin)['status'])"` → `healthy`
- [ ] Commit message references what shipped
