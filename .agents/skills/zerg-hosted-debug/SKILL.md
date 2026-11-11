---
name: zerg-hosted-debug
description: Debug a live hosted Longhouse tenant (a hosted Runtime Host): a session's served state, hosted 502s, slow startup, large tenant SQLite or WAL, disk pressure, tenant container health, hosted auth, or control-plane-managed tenants. Not for local `make dev` (use zerg-dev).
---

# Zerg Hosted Debug

For managed-session latency and badge questions start with `managed-session-debug`.
Longhouse history is the product: never prune archived session data as a recovery
shortcut unless David approves the loss.

## Where things run

The workspace `AGENTS.md` (private) names the hosts, ssh aliases and compose
directories. In this skill `<runtime-host>` is the ssh alias of the host that runs
tenant containers (`longhouse-<tenant>`, data under
`/var/app-data/longhouse/<tenant>`, mounted at `/data`). The control plane runs on a
separate host and drives tenant containers through that host's host agent.
Canary and QA tenants (`release-canary-a`, `hosted-qa-a`) run on the same host. Its
`/tmp` is a small RAM disk, so keep scratch elsewhere, and `rg` is not installed;
use `grep` in remote commands.

Tenant files in `/data`: `longhouse-live.db` (the live catalog, the authoritative
served-state store and by far the largest file), `longhouse.db` (archive database,
`DATABASE_URL`, the target of `db doctor`, `db optimize` and `migrate`), plus their
`-wal`/`-shm` files and the sealed `archive/` objects. Do not assume the generic VPS
`/var/lib/docker/data/...` layout.

## Start here

```bash
bash scripts/ops/hosted-session-debug.sh --subdomain <tenant> --session <id> --limit 20
bash scripts/ops/hosted-session-debug.sh --subdomain <tenant> --session <id> --logs   # add tenant log tail
bash scripts/ops/hosted-session-debug.sh --subdomain <tenant> --session <id> --json
```

It resolves the tenant through the control plane, opens `longhouse-live.db`
read-only over ssh, summarizes the session's `live_sessions`, `live_runtime_state`,
`live_interaction_requests`, `live_timeline_cards` and client-render observations
(a database without live tables falls back to the legacy tables), and summarizes
WriteSerializer pressure and request counts from tenant logs. It needs
`CONTROL_PLANE_ADMIN_TOKEN` or `ADMIN_TOKEN` (Infisical, workspace `AGENTS.md`) and
ssh to the runtime host (`HOSTED_SESSION_DEBUG_SSH_TARGET`). Pass `--subdomain`
or set `LONGHOUSE_DEFAULT_SUBDOMAIN`. Prefer it over
hand-written `ssh` plus guessed DB paths. Auth helpers are in
`scripts/lib/hosted-instance.sh`.

Read in this order: `live_sessions` (execution owner, transport, revisions);
`live_runtime_state` and the timeline card (phase, active tool, terminal state);
`live_interaction_requests` and client-render observations (what is pending, what
the client painted); WriteSerializer summaries (ingest lag vs provider latency);
full logs last. `session_runtime_state` is legacy evidence, not authority.

## Tenant down, 502 or slow to start

Anchor the incident to an exact runtime image SHA, container name and health result,
never "latest".

```bash
curl -fsS https://<tenant>.longhouse.ai/api/readyz
curl -fsS https://<tenant>.longhouse.ai/api/health          # build.commit
ssh <runtime-host> "docker ps -a --filter name=longhouse-<tenant>"
ssh <runtime-host> "df -h /var/app-data && ls -lh /var/app-data/longhouse/<tenant>/*.db*"
ssh <runtime-host> "docker logs --tail 300 longhouse-<tenant> 2>&1 | grep -E 'Startup step|Database initialization step|Application startup|readyz|ERROR'"
```

Startup logs carry `Startup step complete: initialize_database elapsed_ms=...` and
`Database initialization step complete:` lines for `metadata_create_all`,
`residual_agents_migrations` and `agents_fts`. Use those timings as first evidence
before guessing about locks, FTS, migrations or health checks.

## DB doctor and optimize

`longhouse-server` is on PATH in the runtime image. (An image from before the
2026-10-07 entry-point fix has a dead shebang, exit 127; use `python -m zerg.cli.main`.)
Run inside the container, or, when it cannot stay up, in a throwaway container with
the data mounted (use the incident's runtime image SHA where possible):

```bash
ssh <runtime-host> "docker exec longhouse-<tenant> longhouse-server db doctor --json"
ssh <runtime-host> "docker run --rm -v /var/app-data/longhouse/<tenant>:/data \
  -e DATABASE_URL=sqlite:////data/longhouse.db <image> longhouse-server db doctor --json"
```

Fields that matter: `db_bytes`, `wal_bytes`, `disk_free_bytes`/`disk_free_ratio`,
`db_page_size`/`db_page_count`, `db_freelist_count`/`db_freelist_bytes` (reclaimable
by an offline compact), `backup_bytes`/`backup_file_count`/`backup_scan_truncated`
(the scan is capped), `schema.sqlite_stat1_estimated_rows`,
`schema.raw_json_pending_indexes`.

`--deep` runs only indexed backlog counts. `--deep --identity-counts` can scan large
archive tables; use it sparingly.

`db optimize --json` (same two invocation forms) runs `PRAGMA optimize` when
`sqlite_stat1` is missing or stale. It may improve planning; it does not shrink the
file and is not compaction.

## Heavy migrations

Startup schema convergence stays lightweight; historical rewrites are explicit
operator commands. Plan first, apply only after reviewing the plan:

```bash
longhouse-server migrate --database-url sqlite:////data/longhouse.db --no-schema-converge --json   # heavy migrations only
longhouse-server migrate --database-url sqlite:////data/longhouse.db --schema-converge --json      # with lightweight convergence
longhouse-server migrate --database-url sqlite:////data/longhouse.db --apply --json
```

Render branch-count upgrade (`db repair-render-counts`): procedure and maintenance
window rules are in `docs/runbooks/runtime-data-upgrades.md`. On a tenant run it in
the container, keep the cache on the data mount, and never infer readiness from
process health (the API must stop returning `branch_projection_pending`).

## Guardrails

- No `DELETE` on historical archive tables as recovery.
- No live `VACUUM` on a large tenant DB without an offline plan, space check, backup
  and rollback path.
- No broad startup backfills. Startup may create tables, add columns, create indexes
  and verify FTS.
- Change nothing on the runtime host from a diagnosis session beyond the commands above; deploys
  and promotions go through Promote Rings (workspace `AGENTS.md`).
