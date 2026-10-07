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
- open the tenant's live catalog (`longhouse-live.db`) read-only on the host data path
- summarize the session's `live_sessions` row, `live_runtime_state`, `live_interaction_requests`, `live_timeline_cards` and client-render observations (a database without live tables falls back to the legacy `sessions`, `events`, `session_runtime_state` and `session_turns` tables)
- summarize recent WriteSerializer pressure and request counts from tenant logs

Prefer this over ad hoc `ssh` + guessed DB paths + nested heredoc quoting.

## Canonical Paths

- Host data root: `/var/app-data/longhouse/<subdomain>`
- Tenant container mount: `/data`
- Archive DB: `/data/longhouse.db` (`DATABASE_URL`; what `db doctor`, `db optimize` and `migrate` act on)
- Live catalog: `/data/longhouse-live.db`, the sibling every file-backed archive DB has; the authoritative served-state store, and the file `hosted-session-debug.sh` reads

This is an explicit Longhouse exception on the runtime host; do not assume the generic VPS `/var/lib/docker/data/...` layout.

## Auth / Control Plane

The helper expects `CONTROL_PLANE_ADMIN_TOKEN` or `ADMIN_TOKEN`.

It uses the existing repo helper in `scripts/lib/hosted-instance.sh`, which already knows how to:
- resolve a tenant from `control.longhouse.ai`
- mint a hosted login token
- exchange it into a browser cookie jar

## Debug Order

1. Check the `live_sessions` row for execution ownership, managed transport and revisions.
2. Check `live_runtime_state` and the timeline card for current phase, active tool, terminal state and live timestamps. Served state and command authority come from catalogd fact snapshots; the legacy `session_runtime_state` table is evidence, not authority.
3. Check `live_interaction_requests` and the client-render observations to see what is pending and what the client painted.
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

The image puts `longhouse-server` on PATH. Images built before the
2026-10-07 entry-point fix ship it with a dead shebang (exit 127); on those use
`python -m zerg.cli.main` with the same arguments.

When the tenant container is running:

```bash
ssh <runtime-host> "docker exec longhouse-<subdomain> longhouse-server db doctor --json"
```

When the tenant container cannot stay up, run the same command in a temporary
runtime container with tenant data mounted:

```bash
ssh <runtime-host> "docker run --rm \
  -v /var/app-data/longhouse/<subdomain>:/data \
  -e DATABASE_URL=sqlite:////data/longhouse.db \
  <runtime-image> \
  longhouse-server db doctor --json"
```

Use the exact runtime image SHA from the incident when possible. If the goal is
only file diagnostics, any current runtime image with the new CLI is acceptable.

Important fields:

- `db_bytes`, `wal_bytes`: current database and WAL file sizes.
- `disk_free_bytes`, `disk_free_ratio`: remaining disk headroom on the DB volume.
- `db_page_size`, `db_page_count`: logical SQLite page footprint.
- `db_freelist_count`, `db_freelist_bytes`: pages SQLite can reclaim with an offline compact/VACUUM-style operation.
- `backup_bytes`, `backup_file_count`, `backup_scan_truncated`: backup footprint. The scan is capped so it cannot get stuck walking a huge backup tree.
- `schema.sqlite_stat1_estimated_rows`: planner row estimates from the last ANALYZE/optimize.
- `schema.raw_json_pending_indexes`: whether indexed raw JSON backlog counts are safe to run.

`db doctor --deep` only runs indexed backlog counts by default. The expensive
identity backfill counts require a second explicit flag:

```bash
longhouse-server db doctor --json
longhouse-server db doctor --json --deep
longhouse-server db doctor --json --deep --identity-counts
```

Use `--identity-counts` sparingly on large tenants. It can scan archive tables.

## Planner Maintenance

If `sqlite_stat1` is missing or obviously stale, run explicit planner
maintenance:

```bash
ssh <runtime-host> "docker exec longhouse-<subdomain> longhouse-server db optimize --json"
```

For a down container:

```bash
ssh <runtime-host> "docker run --rm \
  -v /var/app-data/longhouse/<subdomain>:/data \
  -e DATABASE_URL=sqlite:////data/longhouse.db \
  <runtime-image> \
  longhouse-server db optimize --json"
```

This runs `PRAGMA optimize`. It may improve query planning. It does not shrink
the database file and must not be described as compaction.

## Heavy Migrations

Startup schema convergence must stay lightweight. Historical rewrites belong in
explicit heavy migrations.

Plan heavy migrations without running startup convergence:

```bash
longhouse-server migrate --database-url sqlite:////data/longhouse.db --no-schema-converge --json
```

Plan with normal lightweight convergence first:

```bash
longhouse-server migrate --database-url sqlite:////data/longhouse.db --schema-converge --json
```

Apply only after reviewing the plan:

```bash
longhouse-server migrate --database-url sqlite:////data/longhouse.db --apply --json
```

Heavy migrations can rewrite large archive tables. Treat them as operator
maintenance, not startup work.

The render branch-count upgrade (`db repair-render-counts`: prepare a verified
cache while the old API still serves, then `--apply` once the updated catalog
writer runs) is documented in
[CONTRIBUTING.md, "Runtime data upgrades"](../../../CONTRIBUTING.md#runtime-data-upgrades).
On a hosted tenant run it in the container (`docker exec ... longhouse-server
db repair-render-counts ...`), keep the cache on the persistent
data mount across container replacement, and use an explicit maintenance window
for a combined Runtime Host: stop the old API and catalog, start the new catalog
alone, apply to zero missing, start the new API, then re-apply to catch the old
writer's final objects. Do not accept the upgraded API while it returns
`branch_projection_pending` for historical data, and never infer readiness from
process health.

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
- Hosted tenant containers are named `longhouse-<subdomain>`
