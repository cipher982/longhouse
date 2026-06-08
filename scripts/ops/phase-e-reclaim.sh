#!/usr/bin/env bash
# Phase E reclaim: clean-store rebuild of the david010 monolith, dropping the
# ~61GB of raw transcript bytes (events.raw_json_z + source_lines.raw_json_z)
# that are now fully archive-backed (both streams 100% verified).
#
# Design (hatch-reviewed): ONE stopped maintenance window. Stop container ->
# checkpoint/quiesce -> build slim DB from the static file (no .backup livelock)
# -> validate -> atomic swap -> start -> smoke -> rsync moved-aside OLD db to NAS.
# The moved-aside OLD db IS the rollback + backup; no separate 117GB copy needed.
#
# Option B: KEEP raw column DEFINITIONS, write sentinels (NULL / '' / 0). SQLite
# copies no raw cell payloads into the new file, so the ~61GB is reclaimed while
# the model + readers stay intact.
#
# IRREVERSIBLE at the swap. Requires David's explicit approval. Run in tmux.
set -euo pipefail

TENANT=david010
C=longhouse-$TENANT
BASE=/var/app-data/longhouse/$TENANT
DB=$BASE/longhouse.db
TS=$(date -u +%Y%m%dT%H%M%SZ)
WORK=$BASE/phase-e-$TS
SLIM=$WORK/longhouse.slim.db
OLD=$BASE/longhouse.db.pre-phase-e-$TS

mkdir -p "$WORK"
IMAGE=$(docker inspect "$C" --format '{{.Config.Image}}')
printf '%s\n' "$IMAGE" > "$WORK/runtime-image.txt"

echo "=== pre-flight ==="
df -h /data "$BASE"
ls -lh "$DB" "$DB"-wal "$DB"-shm "$BASE/archive" 2>/dev/null || true
# NOTE: deliberately NO full-DB quick_check/integrity_check here. On this 117GB
# tenant that scan runs multi-HOUR (the spec explicitly prohibits it) and adds no
# safety the build does not already provide: build-slim runs integrity_check +
# quick_check on the SLIM output (small, fast) and we keep the original DB as
# rollback. Cheap instant diagnostics only.
sqlite3 "$DB" 'PRAGMA journal_mode; PRAGMA page_count; PRAGMA freelist_count;'

echo "=== stop + checkpoint ==="
docker stop --time 60 "$C"
# RESTART GUARD: capture the stopped container's identity + a DB content
# fingerprint. The build below takes ~2h, during which an external actor
# (control-plane reprovision, another agent's deploy) can `docker start` or
# recreate the tenant out from under us — `docker stop` is not a lock. If that
# happens, live ingest resumes and our slim DB becomes a stale snapshot; swapping
# it in would silently lose the gap writes. Before the swap we re-check these and
# ABORT fail-closed if anything changed. (This detects the race; it does not
# prevent it — run only when no reprovision/deploy is expected.)
GUARD_CID="$(docker inspect "$C" --format '{{.Id}}' 2>/dev/null || true)"
GUARD_STARTED="$(docker inspect "$C" --format '{{.State.StartedAt}}' 2>/dev/null || true)"
GUARD_FINGERPRINT="$(sqlite3 "$DB" 'SELECT (SELECT COALESCE(MAX(id),0) FROM events) || ":" || (SELECT COALESCE(MAX(id),0) FROM source_lines) || ":" || (SELECT COUNT(*) FROM sessions);' 2>/dev/null || true)"
echo "guard: cid=${GUARD_CID:0:12} startedAt=$GUARD_STARTED fingerprint=$GUARD_FINGERPRINT"
[ -n "$GUARD_FINGERPRINT" ] || { echo "ABORT: could not capture DB fingerprint; not safe to proceed. Restart: docker start $C"; docker start "$C"; exit 1; }
# Checkpoint+truncate the WAL into the main DB so the build reads a fully
# consistent quiesced file. No full quick_check here (see note above).
sqlite3 "$DB" 'PRAGMA wal_checkpoint(TRUNCATE);'
ls -lh "$DB" "$DB"-wal "$DB"-shm 2>/dev/null || true

echo "=== build slim DB ==="
# build_slim.py is the companion phase-e-build-slim.py (kept beside this script);
# piped into the container's venv python, reading the quiesced DB via the /data mount.
BUILD_SLIM="$(dirname "$0")/phase-e-build-slim.py"
docker run --rm -i \
  -e REQUIRE_RECLAIM_OK=1 \
  -e LONGHOUSE_ARCHIVE_ROOT=/data/archive \
  -v "$BASE:/data" \
  --entrypoint /app/.venv/bin/python \
  "$IMAGE" - "/data/longhouse.db" "/data/phase-e-$TS/longhouse.slim.db" < "$BUILD_SLIM" || {
    echo "SLIM BUILD FAILED — DB untouched, container still stopped. Restart with: docker start $C"; exit 1; }

echo "=== final checks on slim (raw_left is EXPECTED > 0: deliberately-kept uncovered rows) ==="
QC=$(sqlite3 "$SLIM" 'PRAGMA quick_check;')
[ "$QC" = "ok" ] || { echo "SLIM quick_check FAILED: $QC — aborting, DB untouched. Restart: docker start $C"; exit 1; }
sqlite3 "$SLIM" '
SELECT "events", COUNT(*), MIN(id), MAX(id) FROM events;
SELECT "source_lines", COUNT(*), MIN(id), MAX(id) FROM source_lines;
SELECT "events_raw_kept", COUNT(*) FROM events WHERE raw_json_z IS NOT NULL OR (raw_json IS NOT NULL AND raw_json <> "");
SELECT "source_lines_raw_kept", COUNT(*) FROM source_lines WHERE raw_json_z IS NOT NULL OR (raw_json IS NOT NULL AND raw_json <> "");
'
sqlite3 "$SLIM" 'PRAGMA wal_checkpoint(TRUNCATE);'
rm -f "$SLIM-wal" "$SLIM-shm"

echo "=== RESTART GUARD re-check (fail closed before touching the live DB) ==="
# The slim DB is a snapshot from the stop above. If the container was started /
# recreated during the build, the live DB has diverged and the slim is stale —
# swapping it would lose the gap writes. Refuse to swap unless the container is
# STILL the same id, STILL stopped, and the DB fingerprint is UNCHANGED.
NOW_CID="$(docker inspect "$C" --format '{{.Id}}' 2>/dev/null || true)"
NOW_RUNNING="$(docker inspect "$C" --format '{{.State.Running}}' 2>/dev/null || true)"
NOW_STARTED="$(docker inspect "$C" --format '{{.State.StartedAt}}' 2>/dev/null || true)"
NOW_FINGERPRINT="$(sqlite3 "$DB" 'SELECT (SELECT COALESCE(MAX(id),0) FROM events) || ":" || (SELECT COALESCE(MAX(id),0) FROM source_lines) || ":" || (SELECT COUNT(*) FROM sessions);' 2>/dev/null || true)"
echo "guard now: cid=${NOW_CID:0:12} running=$NOW_RUNNING startedAt=$NOW_STARTED fingerprint=$NOW_FINGERPRINT"
GUARD_FAIL=""
[ "$NOW_CID" = "$GUARD_CID" ] || GUARD_FAIL="container recreated (cid changed)"
[ "$NOW_RUNNING" = "false" ] || GUARD_FAIL="container is running again (restarted during build)"
[ "$NOW_STARTED" = "$GUARD_STARTED" ] || GUARD_FAIL="container StartedAt changed (restarted during build)"
[ "$NOW_FINGERPRINT" = "$GUARD_FINGERPRINT" ] || GUARD_FAIL="live DB changed since stop (fingerprint drift)"
if [ -n "$GUARD_FAIL" ]; then
  echo "!!! RESTART GUARD TRIPPED: $GUARD_FAIL"
  echo "Refusing to swap a stale slim DB. Live DB untouched. Discarding slim build."
  rm -rf "$WORK"
  docker start "$C" 2>/dev/null || true   # ensure tenant is up (it may already be)
  echo "ABORTED safely; live DB intact. Re-run only when no reprovision/deploy can restart the tenant."
  exit 1
fi
echo "guard OK — container still stopped, DB unchanged since build start"

echo "=== atomic swap (old DB moved aside = rollback) ==="
# Rollback is self-defensive (never let set -e abort mid-recovery) and idempotent
# regardless of where the swap died: it guarantees the ORIGINAL db ($OLD) ends up
# at the live path ($DB) with no stale slim sidecars. Armed as an ERR/INT/TERM
# trap so a command failure OR a signal (SIGHUP/SIGINT/SIGTERM) during the swap
# triggers recovery. (A hard kill -9 / host reboot still needs the manual
# one-liner printed below — that is the irreducible in-place-swap risk; run in
# tmux on the stable host.)
SWAP_STARTED=0
rollback() {
  set +e
  trap - ERR INT TERM HUP
  echo "!!! SWAP INTERRUPTED — restoring original DB ($OLD -> $DB)"
  docker stop --time 30 "$C" 2>/dev/null
  # If $DB is present and is NOT the original (i.e. slim was installed), set it aside.
  if [ -e "$DB" ] && [ -e "$OLD" ]; then mv "$DB" "$BASE/longhouse.db.slim-failed-$TS" 2>/dev/null; fi
  rm -f "$DB-wal" "$DB-shm" 2>/dev/null
  [ -e "$OLD" ] && mv "$OLD" "$DB" 2>/dev/null
  [ ! -e "$OLD-wal" ] || mv "$OLD-wal" "$DB-wal" 2>/dev/null
  [ ! -e "$OLD-shm" ] || mv "$OLD-shm" "$DB-shm" 2>/dev/null
  docker start "$C" 2>/dev/null
  echo "rolled back; original DB restored at $DB. Manual check: curl -fsS https://david010.longhouse.ai/api/readyz"
  exit 1
}
cat <<MANUAL
MANUAL ROLLBACK (only if this process is hard-killed / host reboots mid-swap):
  docker stop $C
  [ -e "$DB" ] && [ -e "$OLD" ] && mv "$DB" "$BASE/longhouse.db.slim-failed-$TS"
  rm -f "$DB-wal" "$DB-shm"
  [ -e "$OLD" ] && mv "$OLD" "$DB"
  [ -e "$OLD-wal" ] && mv "$OLD-wal" "$DB-wal"; [ -e "$OLD-shm" ] && mv "$OLD-shm" "$DB-shm"
  docker start $C
MANUAL
trap 'rollback' ERR INT TERM HUP
SWAP_STARTED=1
# Guard the initial move too: if the DB move itself fails, restore and bail so
# we never leave the service stopped with the DB stranded at $OLD.
mv "$DB" "$OLD" || rollback
# Sidecars must be GONE from the live path before the slim DB is installed (a
# stale $DB-wal next to the slim DB would corrupt startup). "Absent" is fine; a
# real move failure routes to rollback (which restores $OLD and clears live
# sidecars first), never leaving a contaminated or DB-less live path.
[ ! -e "$DB-wal" ] || mv "$DB-wal" "$OLD-wal" || rollback
[ ! -e "$DB-shm" ] || mv "$DB-shm" "$OLD-shm" || rollback
mv "$SLIM" "$DB" || rollback
chown --reference="$OLD" "$DB" || rollback
chmod --reference="$OLD" "$DB" || rollback

echo "=== start + GATING smoke ==="
docker start "$C" || rollback
sleep 8
curl -fsS https://david010.longhouse.ai/api/readyz || rollback
curl -fsS https://david010.longhouse.ai/api/health || rollback
echo "smoke OK"
# Swap committed + healthy. Disarm the rollback trap so unrelated post-swap
# hiccups (e.g. NAS rsync) don't move the now-live slim DB back to the old one.
trap - ERR INT TERM HUP
ls -lh "$DB" "$OLD"; df -h /data

echo "=== NAS backup of moved-aside OLD db + archive (after smoke) ==="
echo "NAS_DEST=synology:/volume1/homes/drose/longhouse-backups/phase-e-$TS/"
echo "rsync -aH --numeric-ids --info=progress2 $OLD synology:/volume1/homes/drose/longhouse-backups/phase-e-$TS/"
echo "rsync -aH --numeric-ids --info=progress2 $BASE/archive/ synology:/volume1/homes/drose/longhouse-backups/phase-e-$TS/archive/"
echo "=== DONE. Retain $OLD until retention window passes. ==="
