# Session Resume Design: Forum Drop-In Chat

## Overview

Transform Forum from passive session visualization into an interactive session multiplexer.
Click an NPC (Claude session) → chat with that Claude Code session in real-time.

## Architecture

### Turn-by-Turn Resume Pattern

Each user message spawns a fresh Claude Code process with context restoration:

```
User Message
    ↓
Backend: POST /api/sessions/{id}/chat
    ↓
1. Validate session ownership & provider
2. Acquire per-session lock (or 409)
3. Resolve workspace (local or temp clone)
4. Prepare session file from Life Hub
    ↓
5. Spawn: claude --resume {id} -p "message" --output-format stream-json
    ↓
6. Stream SSE events to frontend
    ↓
7. On complete: Ship session back to Life Hub
```

### Key Components

#### Backend

**`routers/session_chat.py`**
- `POST /sessions/{session_id}/chat` - SSE streaming endpoint
- `GET /sessions/{session_id}/lock` - Check lock status
- `DELETE /sessions/{session_id}/lock` - Force release (admin)

**`services/session_continuity.py`**
- `SessionLockManager` - In-memory async locks with TTL
- `WorkspaceResolver` - Clone repo to temp if workspace unavailable
- Existing: `prepare_session_for_resume()`, `ship_session_to_life_hub()`

#### Frontend

**`components/SessionChat.tsx`**
- Message list with streaming assistant response
- Cancel button (AbortController)
- Lock status indicators

**`pages/ForumPage.tsx`**
- Chat mode toggle when Claude session selected
- SessionChat replaces metadata panel in chat mode

### SSE Event Types

| Event | Data | Description |
|-------|------|-------------|
| `system` | `{type, session_id, workspace}` | Session info, status updates |
| `assistant_delta` | `{text, accumulated}` | Streaming text chunks |
| `tool_use` | `{name, id}` | Tool call notification |
| `tool_result` | `{result}` | Tool execution result |
| `error` | `{error, details?}` | Error message |
| `done` | `{exit_code, total_text_length}` | Completion signal |

## Security Considerations

### Path Traversal Prevention
- Workspace path derived server-side from session metadata
- Client never provides workspace path
- Session IDs validated with strict pattern

### Concurrent Access
- Per-session async locks prevent simultaneous resumes
- 409 response when session locked, with fork option (future)
- TTL-based expiration (5 min default) for crash recovery

### Process Management
- Process terminated on client disconnect
- AbortController propagates cancellation
- Cleanup of temp workspaces on completion/error

## Workspace Resolution

Priority order:
1. **Original path exists locally** → Use directly
2. **Git repo in session metadata** → Clone to temp dir
3. **Neither available** → Error (chat-only future option)

Temp workspaces:
- Location: `/tmp/zerg-session-workspaces/session-{id[:12]}`
- Shallow clone (`--depth=1`) for speed
- Cleaned up after chat completion

## Performance Characteristics

Based on lab testing (`scripts/session-resume-lab/`):
- TTFT: ~8-12 seconds (context reload + first token)
- Context growth: ~1.3KB per turn
- Session file: Updated and shipped after each turn

## Future Enhancements

1. **Fork sessions** - Create new session from locked session's state
2. **Chat-only mode** - Allow conversation without workspace (no tools)
3. **Tool execution warnings** - Confirm before destructive operations
4. **Multi-session view** - Chat with multiple sessions in tabs

## API Reference

### POST /api/sessions/{session_id}/chat

Request:
```json
{
  "message": "What files did you modify?"
}
```

Response: SSE stream

Errors:
- 404: Session not found
- 400: Non-Claude session (only Claude sessions resumable)
- 409: Session locked (includes lock_info)
- 500: Internal error

### GET /api/sessions/{session_id}/lock

Response:
```json
{
  "locked": true,
  "holder": "abc123",
  "time_remaining_seconds": 245.5,
  "fork_available": true
}
```
