#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# regen-ws-code.sh – regenerate WebSocket contract artefacts from AsyncAPI.
# ---------------------------------------------------------------------------
# Canonical schema:
#   schemas/ws-protocol-asyncapi.yml
#
# Generated outputs:
#   apps/zerg/backend/zerg/generated/ws_messages.py
#   apps/zerg/frontend-web/src/generated/ws-messages.ts
#   schemas/ws-protocol.schema.json
#   schemas/ws-protocol-v1.json
#
# IMPORTANT: Use `uv run` so the script runs with the backend's Python env
# (PyYAML/jsonschema are available there).
set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
SCHEMA_FILE="$ROOT_DIR/schemas/ws-protocol-asyncapi.yml"

if [[ ! -f "$SCHEMA_FILE" ]]; then
  echo "❌ WebSocket AsyncAPI schema not found at $SCHEMA_FILE" >&2
  exit 1
fi

cd "$ROOT_DIR/apps/zerg/backend"

# Hermetic uv cache/temp inside repo (matches run_backend_tests.sh pattern).
export XDG_CACHE_HOME="$(pwd)/.uv_cache"
export TMPDIR="$(pwd)/.uv_tmp"
mkdir -p "$XDG_CACHE_HOME" "$TMPDIR"

uv run python ../../../scripts/generate-ws-types-modern.py schemas/ws-protocol-asyncapi.yml
