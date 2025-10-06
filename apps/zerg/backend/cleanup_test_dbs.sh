#!/bin/bash
# Clean up worker-specific test database files (supports python/python3)

set -euo pipefail

cd "$(dirname "$0")"

PY=python
if ! command -v "$PY" >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1; then
    PY=python3
  else
    echo "python not found; skipping test DB cleanup" >&2
    exit 0
  fi
fi

"$PY" cleanup_test_dbs.py
