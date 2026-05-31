---
name: zerg-hosted-debug
description: Debug hosted Longhouse instances on the runtime host. Use when investigating a hosted tenant / live prod behavior, control-plane-managed tenants, managed session state, hosted auth, or tenant SQLite state.
---

# Zerg Hosted Debug

Use this when the question is about a live hosted tenant, not local `make dev`.

For hosted 502s, slow Runtime Host startup, large tenant SQLite files, WAL
growth, or disk pressure, use the SQLite path in this skill before touching the
database. Longhouse history is the product value; do not prune archived session
logs as a recovery shortcut unless the maintainer approves data loss.

## Default Path

Start with the repo helper:

```bash
bash scripts/ops/hosted-session-debug.sh --subdomain <subdomain> --session <session-id> --limit 20
bash scripts/ops/hosted-session-debug.sh --subdomain <subdomain> --session <session-id> --logs
bash scripts/ops/hosted-session-debug.sh --subdomain <subdomain> --session <session-id> --json
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

This is an explicit Longhouse exception on the runtime host; do not assume the generic VPS `/var/lib/docker/data/...` layout.

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

## Hosted SQLite Path

Use this path when a hosted Runtime Host is slow to start, returns 502, has a
large `longhouse.db`, or may be under disk pressure.

First verify the live surface and exact runtime build:

```bash
curl -fsS https://<subdomain>.longhouse.ai/api/readyz
curl -fsS https://<subdomain>.longhouse.ai/api/health
```

Check host/container state:

```bash
ssh <runtime-host> "docker ps -a --filter name=longhouse-<subdomain>"
ssh <runtime-host> "df -h /var/app-data && ls -lh /var/app-data/longhouse/<subdomain>/longhouse.db*"
ssh <runtime-host> "docker logs --tail 300 longhouse-<subdomain> 2>&1 | grep -E 'Startup step|Database initialization step|Application startup|readyz|ERROR'"
```

Startup logs should show both coarse and database-specific timings:

- `Startup step complete: initialize_database elapsed_ms=...`
- `Database initialization step complete: metadata_create_all elapsed_ms=...`
- `Database initialization step complete: residual_agents_migrations elapsed_ms=...`
- `Database initialization step complete: agents_fts elapsed_ms=...`

If startup is slow, use these timings as first evidence before guessing about
SQLite locks, FTS, migrations, or container health checks.

## DB Doctor

When the tenant container is running:

```bash
ssh <runtime-host> "docker exec longhouse-<subdomain> python -m zerg.cli.main db doctor --json"
```

When the tenant container cannot stay up, run the same command in a temporary
runtime container with tenant data mounted:

```bash
ssh <runtime-host> "docker run --rm \
  -v /var/app-data/longhouse/<subdomain>:/data \
  -e DATABASE_URL=sqlite:////data/longhouse.db \
  <runtime-image> \
  python -m zerg.cli.main db doctor --json"
```

Use the exact runtime image SHA from the incident when possible. If the goal is
only file diagnostics, any current runtime image with the new CLI is acceptable.

Important fields:

- `db_bytes`, `wal_bytes`: current database and WAL file sizes.
- `disk_free_bytes`, `disk_free_ratio`: remaining disk headroom on the DB volume.
- `db_page_size`, `db_page_count`: logical SQLite page footprint.
- `db_freelist_count`, `db_freelist_bytes`: pages SQLite can reclaim with an offline compact/VACUUM-style operation.
- `backup_bytes`, `backup_file_count`, `backup_scan_truncated`: backup footprint. The scan is capped so Watchman cannot get stuck walking a huge backup tree.
- `schema.sqlite_stat1_estimated_rows`: planner row estimates from the last ANALYZE/optimize.
- `schema.raw_json_pending_indexes`: whether indexed raw JSON backlog counts are safe to run.

`db doctor --deep` only runs indexed backlog counts by default. The expensive
identity backfill counts require a second explicit flag:

```bash
python -m zerg.cli.main db doctor --json
python -m zerg.cli.main db doctor --json --deep
python -m zerg.cli.main db doctor --json --deep --identity-counts
```

Use `--identity-counts` sparingly on large tenants. It can scan archive tables.

## Planner Maintenance

If `sqlite_stat1` is missing or obviously stale, run explicit planner
maintenance:

```bash
ssh <runtime-host> "docker exec longhouse-<subdomain> python -m zerg.cli.main db optimize --json"
```

For a down container:

```bash
ssh <runtime-host> "docker run --rm \
  -v /var/app-data/longhouse/<subdomain>:/data \
  -e DATABASE_URL=sqlite:////data/longhouse.db \
  <runtime-image> \
  python -m zerg.cli.main db optimize --json"
```

This runs `PRAGMA optimize`. It may improve query planning. It does not shrink
the database file and must not be described as compaction.

## Heavy Migrations

Startup schema convergence must stay lightweight. Historical rewrites belong in
explicit heavy migrations.

Plan heavy migrations without running startup convergence:

```bash
python -m zerg.cli.main migrate --database-url sqlite:////data/longhouse.db --no-schema-converge --json
```

Plan with normal lightweight convergence first:

```bash
python -m zerg.cli.main migrate --database-url sqlite:////data/longhouse.db --schema-converge --json
```

Apply only after reviewing the plan:

```bash
python -m zerg.cli.main migrate --database-url sqlite:////data/longhouse.db --apply --json
```

Heavy migrations can rewrite large archive tables. Treat them as operator
maintenance, not startup work.

## Watchman Evidence

Ops Watchman records `db_file_stats` observations with the same DB/disk/page
fields used by `db doctor`. In an incident, check recent Watchman observations
before manually sampling the host; they should show whether DB size, WAL size,
backup footprint, or disk free changed before the outage.

## SQLite Guardrails

- Do not run `DELETE` against historical archive tables as a recovery shortcut.
- Do not run live `VACUUM` on a very large hosted tenant DB without an explicit
  offline plan, space check, backup, and rollback path.
- Do not rely on generic "latest" deploy claims. Anchor every hosted incident to
  an exact runtime image SHA, tenant container name, and health endpoint result.
- Do not add broad startup backfills. Startup may create tables, add missing
  columns, create indexes, and verify FTS. Historical archive rewrites must be
  explicit commands.

## Host Notes

- SSH host is the runtime host (a configured SSH alias)
- `rg` is not guaranteed on the server; use `grep` in remote log commands
- Coolify app container names are hashy, but hosted tenant containers are stable `longhouse-<subdomain>`
