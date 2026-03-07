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

# Always use in-memory SQLite for lite tests - zero disk I/O
export DATABASE_URL="sqlite://"

# Required by zerg/utils/crypto.py at import time (module-level Fernet init).
# Generate a throwaway Fernet-compatible key with the stdlib so CI does not
# depend on cryptography before uv creates the test venv.
if [ -z "${FERNET_SECRET:-}" ]; then
    export FERNET_SECRET="$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
fi

uv run --extra dev pytest tests_lite/ -p no:warnings --tb=short "$@"
