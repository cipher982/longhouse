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

# Always use in-memory SQLite for lite tests — zero disk I/O
export DATABASE_URL="sqlite://"

# Required by zerg/utils/crypto.py at import time (module-level Fernet init).
# Tests that use crypto functionality (e.g. test_email_config.py) will fail
# to collect without this, causing pytest INTERNALERROR (SystemExit during import).
export FERNET_SECRET="${FERNET_SECRET:-Mj7MFJspDPjiFBGHZJ5hnx70XAFJ_En6ofIEhn3BoXw=}"

uv run --extra dev pytest tests_lite/ -p no:warnings --tb=short "$@"
