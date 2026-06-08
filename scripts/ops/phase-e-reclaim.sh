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
  -v "$BASE:/data" \
  --entrypoint /app/.venv/bin/python \
  "$IMAGE" - "/data/longhouse.db" "/data/phase-e-$TS/longhouse.slim.db" < "$BUILD_SLIM"

echo "=== final checks on slim ==="
sqlite3 "$SLIM" '
PRAGMA quick_check;
SELECT "events", COUNT(*), MIN(id), MAX(id) FROM events;
SELECT "source_lines", COUNT(*), MIN(id), MAX(id) FROM source_lines;
SELECT "events_raw_left", COUNT(*) FROM events WHERE raw_json IS NOT NULL OR raw_json_z IS NOT NULL OR raw_json_codec <> 0;
SELECT "source_lines_raw_left", COUNT(*) FROM source_lines WHERE raw_json <> "" OR raw_json_z IS NOT NULL OR raw_json_codec <> 0;
'
sqlite3 "$SLIM" 'PRAGMA wal_checkpoint(TRUNCATE);'
rm -f "$SLIM-wal" "$SLIM-shm"

echo "=== atomic swap ==="
mv "$DB" "$OLD"
[ ! -e "$DB-wal" ] || mv "$DB-wal" "$OLD-wal"
[ ! -e "$DB-shm" ] || mv "$DB-shm" "$OLD-shm"
mv "$SLIM" "$DB"
chown --reference="$OLD" "$DB"
chmod --reference="$OLD" "$DB"

echo "=== start + smoke ==="
docker start "$C"
sleep 5
curl -fsS https://david010.longhouse.ai/api/readyz || true
curl -fsS https://david010.longhouse.ai/api/health || true
ls -lh "$DB" "$OLD"; df -h /data

echo "=== NAS backup of moved-aside OLD db + archive (after smoke) ==="
echo "NAS_DEST=synology:/volume1/homes/drose/longhouse-backups/phase-e-$TS/"
echo "rsync -aH --numeric-ids --info=progress2 $OLD synology:/volume1/homes/drose/longhouse-backups/phase-e-$TS/"
echo "rsync -aH --numeric-ids --info=progress2 $BASE/archive/ synology:/volume1/homes/drose/longhouse-backups/phase-e-$TS/archive/"
echo "=== DONE. Retain $OLD until retention window passes. ==="
