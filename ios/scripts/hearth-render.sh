#!/usr/bin/env bash
# Render the iOS timeline Hearth offscreen on this Mac's GPU: the app's own
# HearthHeat, HearthSimulation and shader source, driven by fixed scenarios
# (busy, pilot, subagents, waiting, idle, cold, ended) on a deterministic clock.
#
#   ios/scripts/hearth-render.sh [--out DIR] [--light] [--low-power]
#       [--reduced-motion] [--seconds 12] [--fps 30] [--scale 3] [--label name]
#   ios/scripts/hearth-render.sh --bench 12      GPU/CPU cost with 12 busy fires, no video
#
# Writes <label>.mp4, three PNG stills and <label>-stats.json (GPU and CPU
# encode time per frame) to --out (default /tmp/agents/hearth-render).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HEARTH="$ROOT/Sources/LonghouseApp/Inbox/Hearth"
BUILD="${TMPDIR:-/tmp}/hearth-render-build"
mkdir -p "$BUILD"
swiftc -O -module-name HearthRender -o "$BUILD/hearth-render" \
  "$HEARTH/HearthHeat.swift" "$HEARTH/HearthShaderSource.swift" "$HEARTH/HearthSimulation.swift" \
  "$ROOT/scripts/hearth-render/main.swift"
exec "$BUILD/hearth-render" "$@"
