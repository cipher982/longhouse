#!/bin/bash

# Zerg Backend Lite Test Runner
# =============================
# SQLite-only, fast-running suite for OSS lite mode.
# Does not require Docker or Postgres.

set -euo pipefail

# Use repository-local cache/temp for uv reliability
export XDG_CACHE_HOME="$(pwd)/.uv_cache"
test_uid="${UID:-$(id -u)}"
if [ -n "${LONGHOUSE_TEST_TMPDIR:-}" ]; then
    export TMPDIR="$LONGHOUSE_TEST_TMPDIR"
    cleanup_test_tmpdir=0
else
    export TMPDIR="$(mktemp -d "/tmp/longhouse-tests-${test_uid}-XXXXXX")"
    cleanup_test_tmpdir=1
fi
export UV_CACHE_DIR="$XDG_CACHE_HOME"

cleanup_runner_tmpdir() {
    if [ "$cleanup_test_tmpdir" -ne 1 ]; then
        return
    fi
    case "$TMPDIR" in
        "/tmp/longhouse-tests-${test_uid}-"*) rm -r -- "$TMPDIR" 2>/dev/null || true ;;
    esac
}
trap cleanup_runner_tmpdir EXIT

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

mkdir -p "$XDG_CACHE_HOME" "$TMPDIR"

export TESTING=1

# Always use in-memory SQLite for lite tests - zero disk I/O
export DATABASE_URL="sqlite://"

# Pin auth-related test env before pytest imports any modules. Some test files
# call os.environ.setdefault(...) at import time, so leaving these unset makes
# collection order change the effective secrets.
export JWT_SECRET="${JWT_SECRET:-test-jwt-secret-1234}"
export INTERNAL_API_SECRET="${INTERNAL_API_SECRET:-test-internal-secret-1234}"
export GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-test-google-client-id}"
export GOOGLE_CLIENT_SECRET="${GOOGLE_CLIENT_SECRET:-test-google-client-secret}"

# Production historical admission intentionally samples the real storage
# filesystem. The lite suite must not turn the developer/CI host's ambient disk
# pressure into unrelated archive-test failures; dedicated admission tests set
# explicit thresholds and mock disk usage themselves.
export LONGHOUSE_HISTORICAL_MIN_FREE_BYTES=0
export LONGHOUSE_HISTORICAL_MIN_FREE_RATIO=0

# Required by zerg/utils/crypto.py at import time (module-level Fernet init).
# Generate a throwaway Fernet-compatible key with the stdlib so CI does not
# depend on cryptography before uv creates the test venv.
if [ -z "${FERNET_SECRET:-}" ]; then
    export FERNET_SECRET="$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
fi

pytest_args=(tests_lite/ -p no:warnings --tb=short)

has_xdist_arg=0
for arg in "$@"; do
    case "$arg" in
        -n|--numprocesses|--numprocesses=*)
            has_xdist_arg=1
            ;;
    esac
done

if [ "$has_xdist_arg" -eq 0 ]; then
    # The suite's SQLite/process-heavy tests are not xdist-safe on cube: even
    # four workers are killed and leave the scheduler hung until the job
    # timeout. Serial is deterministic and completes inside the CI budget;
    # developers can still opt into fanout explicitly while debugging.
    xdist_workers="${PYTEST_XDIST_WORKERS:-0}"
    case "$xdist_workers" in
        ""|0|false|False|FALSE|off|Off|OFF|no|No|NO)
            ;;
        *)
            pytest_args+=(-n "$xdist_workers" --dist=loadfile)
            ;;
    esac
fi

uv run --extra dev pytest "${pytest_args[@]}" "$@"
