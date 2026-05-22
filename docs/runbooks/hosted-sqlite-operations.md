# Hosted SQLite Operations Runbook

Use this for hosted Longhouse tenant incidents where the Runtime Host is slow to
start, returns 502, has a large `longhouse.db`, or may be under disk pressure.

Longhouse owns the full session mirror. Do not prune historical `events`,
`source_lines`, or `session_observations` as a recovery shortcut unless David
explicitly approves data loss.

## First Checks

Verify the live surface and exact runtime build:

```bash
curl -fsS https://<subdomain>.longhouse.ai/api/readyz
curl -fsS https://<subdomain>.longhouse.ai/api/health
```

On `zerg`, hosted tenant containers are named `longhouse-<subdomain>` and tenant
data lives under `/var/app-data/longhouse/<subdomain>`.

```bash
ssh zerg "docker ps -a --filter name=longhouse-<subdomain>"
ssh zerg "df -h /var/app-data && ls -lh /var/app-data/longhouse/<subdomain>/longhouse.db*"
ssh zerg "docker logs --tail 300 longhouse-<subdomain> 2>&1 | grep -E 'Startup step|Database initialization step|Application startup|readyz|ERROR'"
```

Startup logs should show both coarse and database-specific timings:

- `Startup step complete: initialize_database elapsed_ms=...`
- `Database initialization step complete: metadata_create_all elapsed_ms=...`
- `Database initialization step complete: residual_agents_migrations elapsed_ms=...`
- `Database initialization step complete: agents_fts elapsed_ms=...`

If startup is slow, these timings are the first evidence to use before guessing
about SQLite locks, FTS, migrations, or container health checks.

## DB Doctor

When the tenant container is running:

```bash
ssh zerg "docker exec longhouse-<subdomain> python -m zerg.cli.main db doctor --json"
```

When the tenant container cannot stay up, run the same command in a temporary
runtime container with the tenant data mounted:

```bash
ssh zerg "docker run --rm \
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
python -m zerg.cli.main db doctor --json --deep
python -m zerg.cli.main db doctor --json --deep --identity-counts
```

Use `--identity-counts` sparingly on large tenants. It can scan archive tables.

## Planner Maintenance

If `sqlite_stat1` is missing or obviously stale, run explicit planner
maintenance:

```bash
ssh zerg "docker exec longhouse-<subdomain> python -m zerg.cli.main db optimize --json"
```

For a down container:

```bash
ssh zerg "docker run --rm \
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

## Guardrails

- Do not run `DELETE` against historical archive tables as a recovery shortcut.
- Do not run live `VACUUM` on a very large hosted tenant DB without an explicit
  offline plan, space check, backup, and rollback path.
- Do not rely on generic "latest" deploy claims. Anchor every hosted incident to
  an exact runtime image SHA, tenant container name, and health endpoint result.
- Do not add broad startup backfills. Startup may create tables, add missing
  columns, create indexes, and verify FTS. Historical archive rewrites must be
  explicit commands.
