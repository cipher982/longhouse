# Zerg Ops Backup/Restore Spec (First Principles)

**Date:** 2026-03-03
**Owner:** zerg repo

## 1. Problem

We need one reliable operational path for Longhouse instance durability:

1. Create consistent SQLite backups for every active instance.
2. Verify those backups are actually restorable.
3. Keep local disk bounded with predictable retention.
4. Keep an offsite copy without embedding personal infrastructure details in repo code.

## 2. What We Build

A single script (`zerg-ops`) with an opinionated contract:

1. Discover instances from running `longhouse-*` containers (fallback: all DB dirs).
2. Snapshot each live SQLite DB using SQLite backup API.
3. Compress + manifest each snapshot.
4. Restore-verify every snapshot (`hash`, `integrity_check`, row-count parity).
5. Keep latest `N` snapshots per instance.
6. Prune stale unmanaged raw `longhouse*.db` dumps so one-off manual backups do not silently fill the backup volume.
7. Optionally sync snapshot+manifest offsite via neutral SSH alias (`longhouse-offsite`).
8. Monitor freshness + offsite parity + backup-volume usage and fail loudly when broken.

No env-file config surface. Operational defaults live in code.

## 3. Authoritative Data Scope

Must protect:

- `/var/lib/docker/data/longhouse/<instance>/longhouse.db`

Everything else is derived/replaceable.

## 4. Contract (Per Instance)

1. Create consistent snapshot with `sqlite3.Connection.backup()`.
2. Record manifest (`timestamp`, `source path`, sizes, `snapshot_sha256`, selected row counts, `verified_restore`).
3. Compress snapshot (`zstd` preferred, `gzip` fallback).
4. Decompress + verify byte hash + `PRAGMA integrity_check` + row counts.
5. Persist archive + matching manifest.
6. Prune older local snapshots beyond retention.
7. Prune stale unmanaged raw `longhouse*.db` dumps older than the local grace window.
8. Sync archive+manifest to offsite alias when enabled.

## 5. Command Surface

- `zerg-ops run` — backup + verify + prune + cleanup + docker prune + report
- `zerg-ops backup` — backup + verify + prune + cleanup
- `zerg-ops verify` — verify latest snapshot per instance
- `zerg-ops monitor` — freshness + offsite artifact/size + backup-volume checks
- `zerg-ops cleanup` — prune/cleanup + raw-backup cleanup + docker prune + report
- `zerg-ops report` — disk + backup inventory

Minimal scoped flags:

- `--instance <name>` (repeatable)
- `--no-offsite`
- test-only path overrides: `--live-root`, `--backup-root`, `--tmp-backup-dir`, `--backup-volume-warn-pct`

## 6. Offsite Design

Repo code never stores personal IPs/hostnames/paths.

Offsite target is a neutral SSH alias in code: `longhouse-offsite`.
Host-level SSH config maps that alias to real infrastructure.

Example host SSH config (not in repo):

```sshconfig
Host longhouse-offsite
  HostName <offsite-host-or-tailnet-name>
  User <backup-user>
  IdentityFile /root/.ssh/<key>
  IdentitiesOnly yes
```

## 7. Acceptance Criteria

1. Multi-instance backup works in one run.
2. Restore verification fails hard on corruption/mismatch.
3. Retention prunes to exactly configured count.
4. Stale unmanaged raw `longhouse*.db` dumps are removed automatically.
5. Local contract test passes: `make test-zerg-ops-backup`.
6. Monitor fails when backup freshness, offsite parity, or backup-volume usage is broken.

## 8. Rollout + Verify

```bash
# local contract
make test-zerg-ops-backup

# deploy to host
sudo install -m 0755 scripts/zerg-ops.sh /usr/local/bin/zerg-ops

# configure host ssh alias (outside git)
# /root/.ssh/config -> Host longhouse-offsite ...

# run + verify
sudo /usr/local/bin/zerg-ops backup
sudo /usr/local/bin/zerg-ops verify
sudo /usr/local/bin/zerg-ops monitor
sudo /usr/local/bin/zerg-ops report
```

Common failure triage:

1. `verify hash mismatch` or `integrity_check failed`
   - Treat latest backup as invalid; keep prior snapshots and investigate DB/storage health.
2. `offsite artifacts missing` or `offsite_size_mismatch`
   - Local backup remains valid; fix SSH alias/network/offsite storage and rerun `monitor`.
