# Claude Code Hooks for Live Commis Visibility

This directory contains Claude Code hooks that stream tool events from workspace commis back to Longhouse in real-time.

## How It Works

When a workspace commis runs via `hatch` (which uses `claude --print`):

1. Claude Code loads hooks from `~/.claude/settings.json`
2. On each tool call, hooks fire and POST to Longhouse API
3. Longhouse emits SSE events to the frontend
4. User sees live tool calls in the UI

```
hatch (claude --print)
    │
    ├── PreToolUse hook  → POST /api/internal/commis/tool_event (started)
    ├── PostToolUse hook → POST /api/internal/commis/tool_event (completed)
    └── PostToolUseFailure → POST /api/internal/commis/tool_event (failed)
```

## Hooks Configured

| Event | Purpose | Data Sent |
|-------|---------|-----------|
| `PreToolUse` | Tool call starting | tool_name, tool_input |
| `PostToolUse` | Tool call succeeded | tool_name, tool_input, tool_response |
| `PostToolUseFailure` | Tool call failed | tool_name, tool_input, error |

## Deployment

Deploy hooks to the zerg server:

```bash
./scripts/deploy-hooks.sh
```

This copies the hooks to `~/.claude/` on zerg and sets up the required environment.

## Environment Variables

Set these on the zerg server (passed by commis_job_processor.py):

```bash
# Required - set by Longhouse when spawning commis
LONGHOUSE_CALLBACK_URL="http://localhost:47300"  # Or prod URL
COMMIS_JOB_ID="123"                              # The job being executed
COMMIS_CALLBACK_TOKEN="xxx"                      # Optional auth token

# Hooks directory (set by deploy script)
CLAUDE_HOOKS_DIR="$HOME/.claude/hooks"
```

## Testing Locally

```bash
# Simulate a PostToolUse hook call
echo '{"hook_event_name": "PostToolUse", "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": "file1.txt\nfile2.txt"}' | \
  LONGHOUSE_CALLBACK_URL="http://localhost:47300" \
  COMMIS_JOB_ID="test-123" \
  python3 config/claude-hooks/scripts/tool_event.py
```

## Disabling Hooks

If hooks cause issues, they can be disabled:

1. **Per-session**: Set `LONGHOUSE_CALLBACK_URL=""` when spawning commis
2. **Globally on zerg**: Remove `~/.claude/settings.json` or set `"disableAllHooks": true`

## Debugging

Check hook execution:

```bash
# On zerg server
tail -f ~/.claude/logs/claude.log

# Hook script errors go to stderr (visible in claude.log)
```

## Security Notes

- Hooks only POST to the configured callback URL
- Callback token provides authentication
- Large tool responses are truncated (10KB max)
- Hook failures don't block Claude execution (async + exit 0)
