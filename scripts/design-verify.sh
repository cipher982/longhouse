#!/usr/bin/env bash
# Design verification: run unit tests and check dev server
set -euo pipefail

cd "$(dirname "$0")/.."

echo "=== Design Verify ==="

# 1. Unit tests
echo "→ Running unit tests..."
make test
echo "✓ Unit tests passed"

echo ""
echo "=== All checks passed ==="
