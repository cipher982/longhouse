#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# validate-asyncapi.sh – sanity-check schemas/ws-protocol-asyncapi.yml.
# ---------------------------------------------------------------------------
# Mirrors the schema path used by scripts/regen-ws-code.sh. Uses `uv run`
# so the backend Python environment (PyYAML) is available.

set -euo pipefail

ROOT_DIR="$(git rev-parse --show-toplevel)"
SPEC_FILE="$ROOT_DIR/schemas/ws-protocol-asyncapi.yml"

if [[ ! -f "$SPEC_FILE" ]]; then
  echo "❌ AsyncAPI spec not found at $SPEC_FILE" >&2
  exit 1
fi

cd "$ROOT_DIR/apps/zerg/backend"

# Use the backend uv env so PyYAML is available.
export XDG_CACHE_HOME="$(pwd)/.uv_cache"
export TMPDIR="$(pwd)/.uv_tmp"
mkdir -p "$XDG_CACHE_HOME" "$TMPDIR"

uv run python - <<'PY'
from __future__ import annotations

from pathlib import Path

import yaml

root = Path.cwd().resolve().parents[2]  # apps/zerg/backend -> repo root
spec = root / "schemas" / "ws-protocol-asyncapi.yml"

with spec.open("r", encoding="utf-8") as f:
    data = yaml.safe_load(f)

if not isinstance(data, dict):
    raise SystemExit("❌ AsyncAPI schema is not a YAML mapping")

version = data.get("asyncapi")
if version != "3.0.0":
    raise SystemExit(f"❌ Expected asyncapi: 3.0.0, found: {version!r}")

components = data.get("components") or {}
messages = (components.get("messages") or {})
schemas = (components.get("schemas") or {})

if not messages:
    raise SystemExit("❌ components.messages is empty/missing")
if not schemas:
    raise SystemExit("❌ components.schemas is empty/missing")

print(f"✅ AsyncAPI schema OK (messages={len(messages)}, schemas={len(schemas)})")
PY
