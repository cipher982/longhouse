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
sqlite3 "$DB" 'PRAGMA journal_mode; PRAGMA page_count; PRAGMA freelist_count; PRAGMA quick_check;'

echo "=== stop + checkpoint ==="
docker stop --time 60 "$C"
sqlite3 "$DB" 'PRAGMA wal_checkpoint(TRUNCATE); PRAGMA quick_check;'
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

echo "=== atomic swap (old DB moved aside = rollback) ==="
# Rollback if anything past here fails before the container is confirmed healthy.
rollback() {
  echo "!!! SWAP FAILED — rolling back to $OLD"
  docker stop --time 30 "$C" 2>/dev/null || true
  # Remove any slim DB + its sidecars sitting at the live path, so the restored
  # old DB never inherits stale WAL/SHM from the slim build.
  if [ -e "$DB" ]; then mv "$DB" "$BASE/longhouse.db.slim-failed-$TS"; fi
  rm -f "$DB-wal" "$DB-shm"
  mv "$OLD" "$DB"
  [ ! -e "$OLD-wal" ] || mv "$OLD-wal" "$DB-wal"
  [ ! -e "$OLD-shm" ] || mv "$OLD-shm" "$DB-shm"
  docker start "$C"
  echo "rolled back; original DB restored at $DB"
  exit 1
}
# Guard the initial move too: if the DB move itself fails, restore and bail so
# we never leave the service stopped with the DB stranded at $OLD.
mv "$DB" "$OLD" || { echo "ABORT: could not move DB aside; DB untouched. Restart: docker start $C"; docker start "$C"; exit 1; }
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
ls -lh "$DB" "$OLD"; df -h /data

echo "=== NAS backup of moved-aside OLD db + archive (after smoke) ==="
echo "NAS_DEST=synology:/volume1/homes/drose/longhouse-backups/phase-e-$TS/"
echo "rsync -aH --numeric-ids --info=progress2 $OLD synology:/volume1/homes/drose/longhouse-backups/phase-e-$TS/"
echo "rsync -aH --numeric-ids --info=progress2 $BASE/archive/ synology:/volume1/homes/drose/longhouse-backups/phase-e-$TS/archive/"
echo "=== DONE. Retain $OLD until retention window passes. ==="
