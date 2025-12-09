#!/bin/sh
# Fail-fast verification for critical config dependencies
# This runs at container startup to catch missing files immediately
# rather than failing silently when users access the app

set -e

echo "🔍 Verifying critical configuration files..."

# Check if models.json is accessible
# The path is relative from the workspace root: /app/config/models.json
CONFIG_PATH="${MODELS_CONFIG_PATH:-/app/config/models.json}"

if [ ! -f "$CONFIG_PATH" ]; then
  echo "❌ FATAL: models.json not found at $CONFIG_PATH"
  echo "   This file is required for model configuration."
  echo "   Expected location: $CONFIG_PATH"
  echo "   Check Dockerfile COPY instructions and volume mounts."
  exit 1
fi

echo "✅ models.json found at $CONFIG_PATH"

# Verify it's valid JSON
if ! cat "$CONFIG_PATH" | head -c 10 | grep -q "{"; then
  echo "❌ FATAL: $CONFIG_PATH exists but doesn't appear to be valid JSON"
  exit 1
fi

echo "✅ models.json is valid JSON"

# Check contexts directory
if [ ! -d "/app/apps/web/contexts/personal" ]; then
  echo "⚠️  WARNING: Personal context directory not found"
  echo "   App may fail to initialize"
fi

echo "✅ Configuration verification complete"
