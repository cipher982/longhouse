#!/usr/bin/env bash
# Validates CSS rgba() alpha values are in the 0.0-1.0 range.
# The recolor script once converted "0.08" to "8", making backgrounds
# fully opaque. This hook prevents that class of bug.
set -euo pipefail

BAD=$(grep -rn 'rgba([^)]*,\s*[2-9][0-9]*)' --include='*.css' apps/zerg/frontend-web/src/ 2>/dev/null || true)

if [ -n "$BAD" ]; then
  echo "ERROR: Found rgba() with alpha > 1 (should be 0.0-1.0):"
  echo "$BAD"
  exit 1
fi
