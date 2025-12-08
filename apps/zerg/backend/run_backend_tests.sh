#!/bin/bash


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

# Ensure we run inside *backend/* so uv picks up the correct pyproject.toml
mkdir -p "$XDG_CACHE_HOME" "$TMPDIR"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Run unit tests (excluding integration tests which require real API credentials)
# To run integration tests: pytest tests/integration/ -v
uv run pytest tests/ --ignore=tests/integration/ -p no:warnings --tb=short "$@"
