# Zerg Ops Backup/Restore Spec (First Principles)

**Date:** 2026-03-03
**Owner:** zerg repo

## 1. Problem

`zerg` has two different storage concerns:

1. **Data safety:** Longhouse user data must be recoverable after host/container failures.
2. **Disk pressure:** stale snapshots/tmp/docker artifacts can fill the root disk.

Current state had cleanup automation but no unified, repeatable backup+restore contract.

## 2. What We Are Building

A single operations entrypoint (`zerg-ops`) that handles:

1. Legacy cleanup + disk hygiene
2. SQLite backups for all instance DBs
3. Restore verification on every run
4. Retention pruning
5. Optional offsite sync (Synology/NAS-ready)
6. One report command for operator visibility

One config file controls behavior (`/etc/zerg-ops.env`).

## 3. First-Principles Data Scope

### Authoritative data (must be protected)

- `/var/lib/docker/data/longhouse/<instance>/longhouse.db`

This includes sessions/events/raw JSON and automatically covers schema evolution because we copy full SQLite bytes, not selected tables/columns.

### Non-authoritative data (safe to prune)

- legacy `longhouse.pre-*.db` artifacts
- temporary local snapshot files
- docker build/image/volume cruft

## 4. Design Constraints

1. **Do not over-engineer:** one script + one env file + systemd timer.
2. **Schema-agnostic by default:** no table-specific export logic.
3. **No downtime:** snapshot live DB safely via SQLite backup API.
4. **Verification is mandatory:** backup success requires restore validation.
5. **Offsite is optional:** local backup must succeed even if NAS is unavailable.

## 5. Backup Contract

For each discovered instance directory with `longhouse.db`:

1. Create a consistent SQLite snapshot via backup API (`src.backup(dst)`).
2. Capture snapshot metadata:
   - timestamp
   - source DB path
   - uncompressed size
   - SHA-256 of snapshot bytes
   - key row counts (when tables exist)
3. Compress snapshot (`zstd` preferred, `gzip` fallback).
4. Run restore drill:
   - decompress to temp
   - hash must match manifest
   - `PRAGMA integrity_check` must return `ok`
   - row counts must match manifest counts
5. Persist:
   - `longhouse.<timestamp>.sqlite.<ext>`
   - matching `.manifest.json`
6. Prune old snapshots per retention count.
7. Optionally sync snapshot+manifest to remote target over SSH/rsync.

## 6. Command Surface

- `zerg-ops run`
  Full cycle: cleanup -> backup -> verify -> prune -> docker hygiene -> disk report
- `zerg-ops backup`
  Backup + verify + prune only
- `zerg-ops verify`
  Verify latest snapshot per instance
- `zerg-ops monitor`
  Dead-man switch: check backup freshness and (when enabled) offsite artifact presence/size parity
- `zerg-ops cleanup`
  Legacy artifacts + docker prune
- `zerg-ops report`
  Disk + backup inventory summary

## 7. Config Surface (`/etc/zerg-ops.env`)

Required/primary:

- `LIVE_ROOT` (default `/var/lib/docker/data/longhouse`)
- `BACKUP_ROOT` (default `/var/app-data/longhouse-backups`)
- `KEEP_SNAPSHOTS` (count per instance)
- `VERIFY_ON_BACKUP` (`true|false`)
- `ROOT_WARN_PCT`
- `DOCKER_PRUNE_UNTIL_HOURS`
- `MONITOR_MAX_AGE_HOURS` (default 30)
- `MONITOR_REQUIRE_REMOTE` (`auto` default; `true|false` override)
- `ALERT_WEBHOOK_URL` (optional; sends monitor failure message as JSON `text` + `content`)

Optional remote:

- `REMOTE_SSH_TARGET` (example: `drose@100.98.103.56`)
- `REMOTE_BASE_PATH` (example: `/volume1/drose/backups/zerg-longhouse`)

Optional targeting:

- `INSTANCE_ALLOWLIST` (comma-separated instance names; empty = auto-discover all)
- `DISCOVERY_MODE` (`running` default to backup active `longhouse-*` containers only, `all` for every DB directory)

## 8. Acceptance Criteria

1. Script can back up at least two instance directories in one run.
2. Restore drill runs automatically and fails hard on mismatch/corruption.
3. Retention keeps only configured newest snapshots per instance.
4. `report` provides human-usable state for ops checks.
5. Automated local E2E test validates backup->restore hash equality.

## 9. Rollout

1. Implement script in repo.
2. Add local automated E2E script test.
3. Deploy script to `/usr/local/bin/zerg-ops` on host.
4. Update `/etc/zerg-ops.env` with retention + optional remote settings.
5. Run `zerg-ops run` manually once.
6. Confirm timer-based runs and successful latest verify status.

## 10. Ops Commands / Triage

```bash
# Local contract test
make test-zerg-ops-backup

# On zerg host
sudo zerg-ops run
sudo zerg-ops verify
sudo zerg-ops monitor
sudo zerg-ops report
systemctl status zerg-ops.timer --no-pager
systemctl status zerg-ops-monitor.timer --no-pager
journalctl -u zerg-ops.service -n 200 --no-pager
journalctl -u zerg-ops-monitor.service -n 200 --no-pager
```

Common failure paths:

1. `verify hash mismatch` or `integrity_check failed`
   Treat backup as invalid. Keep prior snapshots, inspect host disk and DB health, re-run `zerg-ops backup`.
2. `permission denied` on live/backup paths
   Fix ownership/permissions on `LIVE_ROOT` and `BACKUP_ROOT`; rerun.
3. Remote sync failures
   Local backup remains valid by design. Fix SSH auth/network separately; do not block local retention/verify.
