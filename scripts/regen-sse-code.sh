#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# regen-sse-code.sh – regenerate SSE event contract artefacts from AsyncAPI.
# ---------------------------------------------------------------------------
# Canonical schema:
#   schemas/sse-events.asyncapi.yml
#
# Generated outputs:
#   apps/zerg/backend/zerg/generated/sse_events.py
#   apps/zerg/frontend-web/src/generated/sse-events.ts
#
# IMPORTANT: Use `uv run` so the script runs with the backend's Python env
# (PyYAML is available there).
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
SCHEMA_FILE="$ROOT_DIR/schemas/sse-events.asyncapi.yml"

if [[ ! -f "$SCHEMA_FILE" ]]; then
  echo "❌ SSE AsyncAPI schema not found at $SCHEMA_FILE" >&2
  exit 1
fi

cd "$ROOT_DIR/apps/zerg/backend"

# Hermetic uv cache/temp inside repo (matches run_backend_tests.sh pattern).
export XDG_CACHE_HOME="$(pwd)/.uv_cache"
export TMPDIR="$(pwd)/.uv_tmp"
mkdir -p "$XDG_CACHE_HOME" "$TMPDIR"

uv run python ../../../scripts/generate-sse-types.py schemas/sse-events.asyncapi.yml
