---
name: zerg-hosted-debug
description: Debug hosted Longhouse instances on zerg. Use when investigating david010/live prod behavior, control-plane-managed tenants, managed session state, hosted auth, or tenant SQLite state.
---

# Zerg Hosted Debug

Use this when the question is about a live hosted tenant, not local `make dev`.

For hosted 502s, slow Runtime Host startup, large tenant SQLite files, WAL
growth, or disk pressure, read `docs/runbooks/hosted-sqlite-operations.md`
before touching the database. Longhouse history is the product value; do not
prune archived session logs as a recovery shortcut.

## Default Path

Start with the repo helper:

```bash
bash scripts/ops/hosted-session-debug.sh --subdomain david010 --session <session-id> --limit 20
bash scripts/ops/hosted-session-debug.sh --subdomain david010 --session <session-id> --logs
bash scripts/ops/hosted-session-debug.sh --subdomain david010 --session <session-id> --json
```

It does the right order automatically:
- resolve the tenant via the control plane
- query the tenant SQLite DB on the host data path
- summarize `sessions`, `events`, runtime `session_observations`, `session_runtime_state`, and `session_turns`
- summarize recent WriteSerializer pressure and request counts from tenant logs

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

1. Check `sessions` for execution ownership, managed transport, revisions, and misleading `ended_at`.
2. Check `session_runtime_state` for current phase, active tool, terminal state, and live timestamps.
3. Check recent `events` and runtime `session_observations` to see what ingested and when.
4. Check WriteSerializer/request-count summaries to distinguish hosted ingest lag from provider-loop latency.
5. Only then tail full logs.

For tenant SQLite health, use the runtime CLI before ad hoc SQL:

```bash
python -m zerg.cli.main db doctor --json
python -m zerg.cli.main db doctor --json --deep
python -m zerg.cli.main db optimize --json
python -m zerg.cli.main migrate --no-schema-converge --json
```

`--deep --identity-counts` may scan archive tables and should be used only when
the row-count cost is acceptable.

## Host Notes

- SSH host is `zerg`
- `rg` is not guaranteed on the server; use `grep` in remote log commands
- Coolify app container names are hashy, but hosted tenant containers are stable `longhouse-<subdomain>`
