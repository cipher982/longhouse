#!/bin/bash

# Zerg Backend Lite Test Runner
# =============================
# SQLite-only, fast-running suite for OSS lite mode.
# Does not require Docker or Postgres.

set -euo pipefail

# Use repository-local cache/temp for uv reliability
export XDG_CACHE_HOME="$(pwd)/.uv_cache"
export TMPDIR="$(pwd)/.uv_tmp"
export UV_CACHE_DIR="$XDG_CACHE_HOME"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p "$XDG_CACHE_HOME" "$TMPDIR"

export TESTING=1

# Default to a local SQLite file if DATABASE_URL is not set
if [ -z "${DATABASE_URL:-}" ]; then
    export DATABASE_URL="sqlite:///${SCRIPT_DIR}/.lite_test.db"
fi

uv run pytest tests_lite/ -p no:warnings --tb=short "$@"
