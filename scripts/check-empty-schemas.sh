#!/bin/bash
set -euo pipefail

# Check for empty OpenAPI response schemas that break type generation

ROOT_DIR="$(git rev-parse --show-toplevel)"
SCHEMA_FILE="$ROOT_DIR/apps/zerg/openapi.json"

if [ ! -f "$SCHEMA_FILE" ]; then
    echo "❌ OpenAPI schema not found: $SCHEMA_FILE"
    echo "   Regenerate with: cd apps/zerg/backend && uv run python -c \"from zerg.main import app; app.openapi()\""
    exit 1
fi

MAX_EMPTY_SCHEMAS="${MAX_EMPTY_SCHEMAS:-36}" # Baseline; lower this over time.
EMPTY_COUNT=$(grep -c '"schema": {}' "$SCHEMA_FILE" || echo 0)

echo "🔍 Checking OpenAPI schema completeness..."
echo "📊 Found $EMPTY_COUNT endpoints with empty response schemas (max allowed: $MAX_EMPTY_SCHEMAS)"

if [ "$EMPTY_COUNT" -gt "$MAX_EMPTY_SCHEMAS" ]; then
    echo "❌ Too many empty response schemas ($EMPTY_COUNT) - type safety compromised"
    echo ""
    echo "🔍 Endpoints with empty schemas:"
    (cd "$ROOT_DIR/apps/zerg/backend" && OPENAPI_SCHEMA_FILE="$SCHEMA_FILE" uv run python - <<'PY'
import json
import os

schema_file = os.environ["OPENAPI_SCHEMA_FILE"]
schema = json.load(open(schema_file, encoding="utf-8"))
count = 0
for path, methods in schema["paths"].items():
    for method, details in methods.items():
        if isinstance(details, dict) and "responses" in details:
            resp_200 = details["responses"].get("200", {})
            json_content = resp_200.get("content", {}).get("application/json", {})
            if json_content.get("schema") == {}:
                print(f"   {method.upper()} {path}")
                count += 1
                if count >= 10:  # Limit output
                    print("   ... and more")
                    raise SystemExit(0)
PY
    )
    echo ""
    echo "💡 Add response_model=SomeModel to these endpoints in apps/zerg/backend/zerg/routers/"
    exit 1
fi

echo "✅ Schema completeness check passed ($EMPTY_COUNT empty schemas within tolerance)"
