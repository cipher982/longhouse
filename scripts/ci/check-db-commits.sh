#!/usr/bin/env bash
# Pre-commit hook: warn (never block) when new db.commit() calls appear in router files.
# Reminds developers to consider WriteSerializer for high-frequency endpoints.

set -euo pipefail

diff_output=$(git diff --cached -- 'server/zerg/routers/*.py' || true)

if [ -z "$diff_output" ]; then
    exit 0
fi

# Look for added lines containing db.commit()
matches=$(echo "$diff_output" | grep '^\+' | grep -v '^\+\+\+' | grep 'db\.commit()' || true)

if [ -n "$matches" ]; then
    echo ""
    echo "WARNING: New db.commit() found in router files."
    echo "For high-frequency endpoints, use WriteSerializer instead."
    echo "See services/write_serializer.py"
    echo ""
    echo "Matched lines:"
    echo "$matches"
    echo ""
fi

# Always exit 0 — this is a warning, never a blocker
exit 0
