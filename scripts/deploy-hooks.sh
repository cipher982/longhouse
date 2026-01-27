#!/usr/bin/env bash
# Deploy Claude Code hooks to zerg server
#
# Usage: ./scripts/deploy-hooks.sh [--dry-run]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
HOOKS_DIR="$PROJECT_ROOT/config/claude-hooks"
REMOTE_HOST="zerg"
REMOTE_CLAUDE_DIR="\$HOME/.claude"
REMOTE_HOOKS_DIR="\$HOME/.claude/hooks"

DRY_RUN=false
if [[ "${1:-}" == "--dry-run" ]]; then
    DRY_RUN=true
    echo "[DRY RUN] Would execute the following:"
fi

echo "Deploying Claude Code hooks to $REMOTE_HOST..."

# Check hooks directory exists
if [[ ! -d "$HOOKS_DIR" ]]; then
    echo "Error: Hooks directory not found: $HOOKS_DIR"
    exit 1
fi

# Validate settings.json
if ! python3 -c "import json; json.load(open('$HOOKS_DIR/settings.json'))" 2>/dev/null; then
    echo "Error: Invalid JSON in settings.json"
    exit 1
fi
echo "✓ settings.json is valid JSON"

if $DRY_RUN; then
    echo ""
    echo "Commands that would run:"
    echo "  ssh $REMOTE_HOST 'mkdir -p ~/.claude/hooks/scripts'"
    echo "  scp -r $HOOKS_DIR/settings.json $REMOTE_HOST:~/.claude/"
    echo "  scp -r $HOOKS_DIR/scripts/* $REMOTE_HOST:~/.claude/hooks/scripts/"
    echo ""
    echo "Environment setup that would be added to ~/.zshrc:"
    echo "  export ZERG_HOOKS_DIR=\"\$HOME/.claude/hooks\""
    exit 0
fi

# Create remote directories
echo "Creating remote directories..."
ssh "$REMOTE_HOST" "mkdir -p ~/.claude/hooks/scripts"

# Copy settings.json
echo "Copying settings.json..."
scp "$HOOKS_DIR/settings.json" "$REMOTE_HOST:~/.claude/"

# Copy hook scripts
echo "Copying hook scripts..."
scp "$HOOKS_DIR/scripts/"*.py "$REMOTE_HOST:~/.claude/hooks/scripts/"

# Make scripts executable
echo "Setting permissions..."
ssh "$REMOTE_HOST" "chmod +x ~/.claude/hooks/scripts/*.py"

# Check if ZERG_HOOKS_DIR is already set in .zshrc
if ssh "$REMOTE_HOST" "grep -q 'ZERG_HOOKS_DIR' ~/.zshrc 2>/dev/null"; then
    echo "✓ ZERG_HOOKS_DIR already configured in .zshrc"
else
    echo "Adding ZERG_HOOKS_DIR to .zshrc..."
    ssh "$REMOTE_HOST" 'echo '\''export ZERG_HOOKS_DIR="$HOME/.claude/hooks"'\'' >> ~/.zshrc'
fi

# Verify deployment
echo ""
echo "Verifying deployment..."
ssh "$REMOTE_HOST" "ls -la ~/.claude/settings.json ~/.claude/hooks/scripts/"

echo ""
echo "✓ Hooks deployed successfully to $REMOTE_HOST"
echo ""
echo "Next steps:"
echo "  1. Set webhook URLs on $REMOTE_HOST:"
echo "     export DISCORD_WEBHOOK_URL='your-webhook-url'"
echo "     export SLACK_WEBHOOK_URL='your-webhook-url'"
echo ""
echo "  2. Test a worker execution to verify hooks fire"
