---
name: zerg-hosted-debug
description: Debug hosted Longhouse instances on zerg. Use when investigating david010/live prod behavior, control-plane-managed tenants, loop inbox cards, turn reviews, hosted auth, or tenant SQLite state.
---

# Zerg Hosted Debug

Use this when the question is about a live hosted tenant, not local `make dev`.

## Default Path

Start with the repo helper:

```bash
scripts/hosted-loop-debug.sh david010
scripts/hosted-loop-debug.sh --subdomain david010 --session <session-id> --limit 5
scripts/hosted-loop-debug.sh --subdomain david010 --logs
```

It does the right order automatically:
- resolve the tenant via the control plane
- mint a hosted browser session cookie
- fetch `/api/oikos/loop-inbox` and `/api/oikos/turn-reviews`
- query `/data/longhouse.db` inside the running tenant container

Prefer this over ad hoc `ssh` + guessed DB paths + nested heredoc quoting.

## Canonical Paths

- Host data root: `/var/app-data/longhouse/<subdomain>`
- Tenant container mount: `/data`
- Tenant DB: `/data/longhouse.db`

This is an explicit Longhouse exception on `zerg`; do not assume the generic VPS `/var/lib/docker/data/...` layout.

## Auth / Control Plane

The helper expects `CONTROL_PLANE_ADMIN_TOKEN` or `ADMIN_TOKEN`.

It uses the existing repo helper in `scripts/lib/hosted-instance.sh`, which already knows how to:
- resolve a tenant from `control.longhouse.ai`
- mint a hosted login token
- exchange it into a browser cookie jar

## Debug Order

1. Check the API view first.
2. If the card is missing, inspect `/api/oikos/turn-reviews` for the same session.
3. If the API and UI disagree, inspect live SQLite inside the tenant container.
4. Only then read logs.

For loop cards specifically, the inbox only shows the latest review per session when:
- `execution_state` is `awaiting_user_approval` or `needs_human`
- `status` is `recorded` or `enqueued`

## Host Notes

- SSH host is `zerg`
- `rg` is not guaranteed on the server; use `grep` in remote log commands
- Coolify app container names are hashy, but hosted tenant containers are stable `longhouse-<subdomain>`
