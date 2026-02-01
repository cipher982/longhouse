#!/usr/bin/env bash
# Deploy Claude Code hooks to zerg server for live commis visibility
#
# Usage: ./scripts/deploy-hooks.sh [--dry-run]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
HOOKS_SOURCE="$PROJECT_ROOT/config/claude-hooks"
REMOTE_HOST="zerg"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "[DRY RUN] Would execute the following:"
fi

echo "Deploying Claude Code hooks to $REMOTE_HOST..."

# Validate source directory
if [[ ! -d "$HOOKS_SOURCE" ]]; then
    echo "Error: Hooks source not found: $HOOKS_SOURCE"
    exit 1
fi

# Validate settings.json
if ! python3 -c "import json; json.load(open('$HOOKS_SOURCE/settings.json'))" 2>/dev/null; then
    echo "Error: Invalid JSON in settings.json"
    exit 1
fi
echo "✓ settings.json is valid"

if $DRY_RUN; then
    echo ""
    echo "Commands that would run:"
    echo "  ssh $REMOTE_HOST 'mkdir -p ~/.claude/hooks/scripts'"
    echo "  scp $HOOKS_SOURCE/settings.json $REMOTE_HOST:~/.claude/"
    echo "  scp $HOOKS_SOURCE/scripts/*.py $REMOTE_HOST:~/.claude/hooks/scripts/"
    echo ""
    echo "Environment that would be added to ~/.zshrc:"
    echo "  export CLAUDE_HOOKS_DIR=\"\$HOME/.claude/hooks\""
    exit 0
fi

# Create remote directories
echo "Creating remote directories..."
ssh "$REMOTE_HOST" "mkdir -p ~/.claude/hooks/scripts"

# Copy settings.json to Claude config location
echo "Copying settings.json..."
scp "$HOOKS_SOURCE/settings.json" "$REMOTE_HOST:~/.claude/"

# Copy hook scripts
echo "Copying hook scripts..."
scp "$HOOKS_SOURCE/scripts/"*.py "$REMOTE_HOST:~/.claude/hooks/scripts/"

# Make scripts executable
echo "Setting permissions..."
ssh "$REMOTE_HOST" "chmod +x ~/.claude/hooks/scripts/*.py"

# Check if CLAUDE_HOOKS_DIR is set in .zshrc
if ssh "$REMOTE_HOST" "grep -q 'CLAUDE_HOOKS_DIR' ~/.zshrc 2>/dev/null"; then
    echo "✓ CLAUDE_HOOKS_DIR already in .zshrc"
else
    echo "Adding CLAUDE_HOOKS_DIR to .zshrc..."
    ssh "$REMOTE_HOST" 'echo '\''export CLAUDE_HOOKS_DIR="$HOME/.claude/hooks"'\'' >> ~/.zshrc'
fi

# Verify deployment
echo ""
echo "Verifying deployment..."
ssh "$REMOTE_HOST" "ls -la ~/.claude/settings.json ~/.claude/hooks/scripts/" 2>/dev/null || true

echo ""
echo "✓ Hooks deployed successfully to $REMOTE_HOST"
echo ""
echo "Hooks will activate when commis runs with these env vars:"
echo "  LONGHOUSE_CALLBACK_URL=<api-url>"
echo "  COMMIS_JOB_ID=<job-id>"
echo "  COMMIS_CALLBACK_TOKEN=<optional-token>"
