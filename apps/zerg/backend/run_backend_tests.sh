#!/bin/bash

# Zerg Backend Legacy Test Runner
# ===============================
# Postgres-heavy legacy suite (enterprise paths).
#
# Supports two database modes:
#   --db-mode=docker   (default) Use testcontainers for ephemeral PostgreSQL
#   --db-mode=external           Use external PostgreSQL with CI_TEST_SCHEMA
#
# For the SQLite-lite suite, use:
#   ./run_backend_tests_lite.sh
#
# Usage:
#   ./run_backend_tests.sh                        # Docker mode (local dev)
#   ./run_backend_tests.sh --db-mode=docker       # Explicit docker mode
#   CI_TEST_SCHEMA=zerg_ci_123 DATABASE_URL=... ./run_backend_tests.sh --db-mode=external

# Some CI environments leave stale or malformed files inside ~/.cache/uv
# (e.g. a *file* named ".git" instead of a directory) which causes uv to
# abort with "Operation not permitted".  Work around this by pointing uv to a
# fresh temporary cache directory so test runs are hermetic.

# Create a temporary (per-run) cache directory inside the repository so we
# avoid permission issues in $HOME which may be read-only inside the sandbox.
# Use repository-local directories for cache and temp to avoid sandbox
# permission issues (e.g. inability to create files under /var/folders on
# macOS runners).
export XDG_CACHE_HOME="$(pwd)/.uv_cache"
export TMPDIR="$(pwd)/.uv_tmp"
export UV_CACHE_DIR="$XDG_CACHE_HOME"

# Ensure we run inside *backend/* so uv picks up the correct pyproject.toml
mkdir -p "$XDG_CACHE_HOME" "$TMPDIR"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Validate external mode requirements
for arg in "$@"; do
    if [[ "$arg" == "--db-mode=external" ]]; then
        if [ -z "$CI_TEST_SCHEMA" ]; then
            echo "❌ External DB mode requires CI_TEST_SCHEMA environment variable"
            echo "   Example: CI_TEST_SCHEMA=zerg_ci_123 DATABASE_URL=... $0 --db-mode=external"
            exit 1
        fi
        if [ -z "$DATABASE_URL" ]; then
            echo "❌ External DB mode requires DATABASE_URL environment variable"
            exit 1
        fi
        echo "📊 Running tests with external Postgres (schema: $CI_TEST_SCHEMA)"
        break
    fi
done

# Run tests (excluding live connector tests which require real API credentials)
# To run live connector tests: uv run pytest tests/integration/test_connectors_live.py -v
# -n auto: parallel execution using all CPU cores (requires pytest-xdist)
uv run pytest tests/ --ignore=tests/integration/test_connectors_live.py -n "${PYTEST_XDIST_COMMIS:-auto}" -p no:warnings --tb=short "$@"
