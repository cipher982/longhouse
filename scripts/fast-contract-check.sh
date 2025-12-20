#!/usr/bin/env bash
set -euo pipefail

# Fast contract validation (current monorepo layout).
# Keep this script **fast** and **offline-safe** so it can run in CI/pre-push.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

echo "🔍 Fast contract check..."

# Validates OpenAPI completeness + does best-effort live checks if backend is up.
(
  cd apps/zerg/frontend-web
  bun run validate:contracts
)

echo "✅ Fast contract check passed"
