# Claude Code Hooks for Zerg Workers

This directory contains Claude Code hook configuration for Zerg worker (commis) execution.

## Overview

When workers run via `claude --print` (headless mode), these hooks provide:

1. **Stop Validation**: LLM-based verification that tasks were completed before exit
2. **Notifications**: Discord/Slack alerts when workers need attention
3. **Session Tracking**: Logging of session starts for debugging
4. **Post-Edit Context**: Reminders to run tests after file modifications

## Hooks Configured

| Event | Type | Purpose |
|-------|------|---------|
| `Stop` | prompt | LLM validates task completion before allowing exit |
| `Notification` | command | Sends alerts via webhooks |
| `PostToolUse` (Edit/Write) | command | Adds context about running tests |
| `SessionStart` | command | Logs session metadata |

## Deployment

Deploy hooks to the zerg server:

```bash
./scripts/deploy-hooks.sh
```

This:
1. Copies `config/claude-hooks/` to `~/.claude/` on zerg
2. Sets up environment variables
3. Validates the configuration

## Environment Variables

Set these on the zerg server (in `.zshrc` or systemd service):

```bash
# Webhook URLs for notifications
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."

# Hooks directory (auto-set by deploy script)
export ZERG_HOOKS_DIR="$HOME/.claude/hooks"

# Optional: disable notifications
export ZERG_NOTIFY_ENABLED="1"
```

## Testing Hooks Locally

```bash
# Test notification script
echo '{"session_id": "test-123", "message": "Test notification"}' | \
  DISCORD_WEBHOOK_URL="your-url" python3 scripts/notify.py

# Test session start script
echo '{"session_id": "test-123", "cwd": "/tmp"}' | python3 scripts/session_start.py
```

## Stop Hook Behavior

The Stop hook uses a **prompt-based** evaluation (runs on Haiku for speed/cost):

- Claude cannot declare "done" until the LLM judge confirms task completion
- Checks: changes made, no unrecovered failures, consistent state
- Returns `{"decision": "stop"}` or `{"decision": "continue", "reason": "..."}`

This catches:
- Premature exits before work is complete
- Silent failures that weren't addressed
- Incomplete implementations

## Adding New Hooks

1. Add hook script to `scripts/`
2. Update `settings.json` with the hook configuration
3. Re-run `deploy-hooks.sh`

## Debugging

Check hook execution on zerg:

```bash
# View recent Claude logs
ssh zerg "tail -f ~/.claude/logs/claude.log"

# Check notification script errors
ssh zerg "cat ~/.claude/hooks/scripts/notify.log"
```
