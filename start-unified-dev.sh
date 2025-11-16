#!/bin/bash
# DEPRECATED: This script is deprecated. Use 'make dev' instead.

echo "⚠️  start-unified-dev.sh is deprecated."
echo ""
echo "Please use:"
echo "  make dev           # Full platform with graceful shutdown"
echo ""
echo "For more options:"
echo "  make help          # See all available commands"
echo ""
echo "Running 'make dev' for you..."
echo ""

exec make dev
