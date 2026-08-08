#!/usr/bin/env bash
# record-all.sh — one command to (re)produce the hermetic V2 terminal recordings.
#
#   fetch pinned binaries + srt  ->  record each provider in its sandbox under
#   srt enforcement (mock LLM lane, zero credentials)  ->  retime + compile on
#   the host  ->  copy committable assets into src/assets/terminal/.
#
# Every provider is recorded at BOTH geometries the compositions consume:
#   100x16  ->  <prov>.*            (hero/steer replay)
#   64x14   ->  <prov>-tile.*       (Beat 1 agent tiles)
#
# Fail-closed: record.py refuses to record unless its srt canary proves
# isolation, and exits nonzero unless every take gate passed (see the take's
# .meta.json "canary"/"gates"), so a broken take stops this script before
# anything is copied. The sanitization grep runs over the cast, retimed cast,
# compiled grid, and meta sidecar.
#
# Usage:
#   scripts/terminal/record-all.sh            # all recordable providers
#   scripts/terminal/record-all.sh claude     # one provider (fetches only it + srt)
#
# Providers: claude (mock+srt), codex (mock+srt) are proven and produce assets.
# opencode/cursor are NOT run here (see REPORT.md: opencode mock-routing
# unresolved; cursor blocked). Pass them explicitly to attempt a raw take.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIDEO="$(cd "$HERE/../.." && pwd)"
TAKES="$HERE/.takes"
ASSETS="$VIDEO/src/assets/terminal"

if [[ $# -gt 0 ]]; then
  PROVIDERS=("$@")
  FETCH_ARGS=()
  for p in "${PROVIDERS[@]}"; do FETCH_ARGS+=(--only "$p"); done
  echo "[record-all] fetching pinned binaries for: ${PROVIDERS[*]} (+ srt)"
  ( cd "$HERE" && uv run fetch.py "${FETCH_ARGS[@]}" )
else
  PROVIDERS=(claude codex)
  echo "[record-all] fetching pinned binaries + srt"
  ( cd "$HERE" && uv run fetch.py )
fi

# geometry = "<cols>x<rows>:<asset suffix>"
GEOMETRIES=("100x16:" "64x14:-tile")

mkdir -p "$ASSETS"
for prov in "${PROVIDERS[@]}"; do
  for geom in "${GEOMETRIES[@]}"; do
    size="${geom%%:*}" suffix="${geom#*:}"
    cols="${size%x*}" rows="${size#*x}"
    name="$prov$suffix"
    echo "[record-all] === $name (${cols}x${rows}) ==="
    mkdir -p "$TAKES/$name"
    cast="$TAKES/$name/take.cast"
    ( cd "$HERE" && uv run record.py --sandbox --provider "$prov" \
        --cols "$cols" --rows "$rows" --out "$cast" )

    retimed="$TAKES/$name/take.retimed.cast"
    grid="$TAKES/$name/take.grid.json"
    meta="${cast%.cast}.meta.json"
    ( cd "$VIDEO" && bun scripts/terminal/retime.ts "$cast" "$retimed" )
    ( cd "$VIDEO" && bun scripts/terminal/compile.ts "$retimed" "$grid" )

    # Sanitization gate: refuse to publish any artifact carrying operator
    # identity — the compiled grid and meta sidecar included.
    for f in "$cast" "$retimed" "$grid" "$meta"; do
      if grep -qiE "davidrose|/Users/|@gmail|@drose|$(hostname -s)" "$f"; then
        echo "[record-all] REFUSING to copy $name: identifying strings found in $f" >&2
        exit 2
      fi
    done
    cp "$cast" "$ASSETS/$name.cast"
    cp "$retimed" "$ASSETS/$name.retimed.cast"
    cp "$grid" "$ASSETS/$name.grid.json"
    cp "$meta" "$ASSETS/$name.meta.json"
    echo "[record-all] $name -> $ASSETS/$name.{cast,retimed.cast,grid.json,meta.json}"
  done
done

echo "[record-all] done. lockfile: $HERE/providers.lock.json"
