#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVER_DIR="$ROOT_DIR/server"
WEB_DIR="$ROOT_DIR/web"

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "Missing required command: $1" >&2
    exit 1
  }
}

require_cmd bun
require_cmd uv
require_cmd python3

echo "🏗️  Building frontend dist for wheel packaging..."
(
  cd "$ROOT_DIR"
  bun install --frozen-lockfile --silent
  cd "$WEB_DIR"
  bun run build >/dev/null
)

echo "📦 Building Longhouse wheel..."
(
  cd "$SERVER_DIR"
  rm -rf dist
  uv build --wheel >/dev/null
)

echo "🧪 Validating wheel archive..."
python3 "$ROOT_DIR/scripts/qa/check-wheel.py" "$SERVER_DIR"/dist/longhouse-*.whl
