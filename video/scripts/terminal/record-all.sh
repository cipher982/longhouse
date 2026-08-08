#!/usr/bin/env bash
# record-all.sh — one command to (re)produce the hermetic V2 terminal recordings.
#
#   fetch pinned binaries + srt  ->  record each provider in its sandbox under
#   srt enforcement (mock LLM lane, zero credentials)  ->  retime + compile on
#   the host  ->  copy committable assets into src/assets/terminal/.
#
# Usage:
#   scripts/terminal/record-all.sh            # all recordable providers
#   scripts/terminal/record-all.sh claude     # one provider
#
# Providers: claude (mock+srt), codex (mock+srt) are proven and produce assets.
# opencode/cursor are NOT run here (see REPORT.md: opencode mock-routing
# unresolved; cursor blocked). Pass them explicitly to attempt a raw take.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VIDEO="$(cd "$HERE/../.." && pwd)"
TAKES="$HERE/.takes"
ASSETS="$VIDEO/src/assets/terminal"

PROVIDERS=("${@:-claude codex}")
# shellcheck disable=SC2206
PROVIDERS=(${PROVIDERS[@]})

echo "[record-all] fetching pinned binaries + srt"
( cd "$HERE" && uv run fetch.py )

mkdir -p "$ASSETS"
for prov in "${PROVIDERS[@]}"; do
  echo "[record-all] === $prov ==="
  mkdir -p "$TAKES/$prov"
  cast="$TAKES/$prov/take.cast"
  ( cd "$HERE" && uv run record.py --sandbox --provider "$prov" --out "$cast" )

  retimed="$TAKES/$prov/take.retimed.cast"
  grid="$TAKES/$prov/take.grid.json"
  ( cd "$VIDEO" && bun scripts/terminal/retime.ts "$cast" "$retimed" )
  ( cd "$VIDEO" && bun scripts/terminal/compile.ts "$retimed" "$grid" )

  # Sanitization gate: refuse to publish a cast carrying operator identity.
  if grep -qiE "davidrose|/Users/|@gmail|@drose|$(hostname -s)" "$cast"; then
    echo "[record-all] REFUSING to copy $prov: identifying strings found in cast" >&2
    exit 2
  fi
  cp "$cast" "$ASSETS/$prov.cast"
  cp "$retimed" "$ASSETS/$prov.retimed.cast"
  cp "$grid" "$ASSETS/$prov.grid.json"
  cp "${cast%.cast}.meta.json" "$ASSETS/$prov.meta.json"
  echo "[record-all] $prov -> $ASSETS/$prov.{cast,retimed.cast,grid.json,meta.json}"
done

echo "[record-all] done. lockfile: $HERE/providers.lock.json"
