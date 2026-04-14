#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

git -C "$ROOT" push
SHA="$(git -C "$ROOT" rev-parse HEAD)"
exec "$ROOT/scripts/ops/ship-monitor.py" --sha "$SHA" "$@"
