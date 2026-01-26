# The Forum - Game UI Spec

## Vision

Replace terminal tab-switching with a spatial game interface. Instead of 5 Claude Code tabs, you have 5 workers walking around a game world. Walk up to a worker to see what they're doing, respond if needed.

## Data Sources

### Life Hub Sessions (Primary)
```sql
agents.sessions:
  - id, project, provider, cwd, git_repo
  - started_at, ended_at
  - user_messages, assistant_messages, tool_calls

agents.events:
  - session_id, timestamp, role, tool_name, content_text
```

### What We Have
- **API**: `GET /api/jarvis/life-hub/sessions` - list sessions with filtering
- **API**: `GET /api/jarvis/life-hub/sessions/{id}/preview` - get recent messages
- **Session Continuity**: `session_continuity.py` - fetch/ship sessions to/from Life Hub
- **SwarmOpsPage**: Attention classification logic (hard/needs/soft/auto)

### What ForumPage Currently Does (Wrong)
- Generates **fake** replay data with random entities
- Only shows live SSE events (useless if you're not watching during activity)
- No connection to real session data

## Entity Model

Each session becomes a worker entity:

```typescript
type ForumWorker = {
  id: string;              // session UUID
  project: string;         // "zerg", "life-hub", etc. → determines room
  provider: string;        // "claude", "codex", "gemini" → visual style
  status: WorkerStatus;    // derived from activity analysis
  attention: AttentionLevel; // hard | needs | soft | auto
  lastActivity: Date;
  lastMessage: string;     // truncated preview
  position: GridPosition;  // placement within project room
};

type WorkerStatus =
  | 'working'    // tool calls happening recently
  | 'thinking'   // assistant message in progress
  | 'waiting'    // asked user a question, no response
  | 'idle'       // no recent activity
  | 'completed'  // session ended normally
  | 'error';     // session ended with error
```

## Attention Detection

Derive from session/event data:

| Signal | Attention Level |
|--------|-----------------|
| `AskUserQuestion` tool called | **needs** |
| Last assistant message ends with `?` | **soft** |
| Session ended with error | **hard** |
| Long time since last event | **soft** |
| Actively using tools | **auto** |
| Session completed successfully | **auto** |

SQL to detect:
```sql
-- Sessions needing attention
SELECT s.id, s.project, s.provider,
  CASE
    WHEN EXISTS (
      SELECT 1 FROM agents.events e
      WHERE e.session_id = s.id AND e.tool_name = 'AskUserQuestion'
      ORDER BY e.timestamp DESC LIMIT 1
    ) THEN 'needs'
    WHEN last_msg.content_text LIKE '%?' THEN 'soft'
    WHEN s.ended_at IS NULL AND last_event.timestamp < NOW() - INTERVAL '5 minutes' THEN 'soft'
    ELSE 'auto'
  END as attention
FROM agents.sessions s
LEFT JOIN LATERAL (
  SELECT content_text, timestamp FROM agents.events
  WHERE session_id = s.id AND role = 'assistant' AND content_text IS NOT NULL
  ORDER BY timestamp DESC LIMIT 1
) last_msg ON true
LEFT JOIN LATERAL (
  SELECT timestamp FROM agents.events
  WHERE session_id = s.id
  ORDER BY timestamp DESC LIMIT 1
) last_event ON true
WHERE s.started_at > NOW() - INTERVAL '24 hours';
```

## Room Layout

Group workers by project:

```
┌─────────────────────────────────────────────────────┐
│                    THE FORUM                        │
├──────────────┬──────────────┬──────────────────────┤
│    ZERG      │   LIFE-HUB   │       MISC           │
│   🤖 🤖 🤖   │    🤖 🤖     │        🤖            │
│   claude     │   codex      │      claude          │
│   codex      │   claude     │                      │
├──────────────┴──────────────┴──────────────────────┤
│                   SAURON            HDR            │
│                    🤖               🤖             │
└─────────────────────────────────────────────────────┘
```

Each room = a project. Workers positioned within their project room.

## Interaction Model

### MVP (View Only)
1. See all active/recent sessions as workers
2. Visual indicators for attention level (color-coded auras)
3. Click worker → see conversation preview
4. User then goes to terminal to respond

### Future (Full Control)
1. Click worker → load session into Zerg workspace
2. Respond from within the Forum UI
3. Worker continues with your response
4. Uses `session_continuity.prepare_session_for_resume()`

## Implementation Plan

### Phase 1: Wire Real Data
1. Remove replay/demo system from ForumPage
2. Add `useLifeHubSessions` hook that calls `/api/jarvis/life-hub/sessions`
3. Map sessions → ForumEntity with position/status/attention
4. Poll for updates (every 5-10 seconds)

### Phase 2: Attention Classification
1. Add attention detection to backend (new endpoint or extend existing)
2. Return attention level with session data
3. Visual indicators on canvas (auras, icons)

### Phase 3: Interaction
1. Click worker → show conversation preview panel
2. Use existing `/life-hub/sessions/{id}/preview` endpoint
3. "Open in Terminal" button (later: "Take Over" button)

### Phase 4: Real-time Updates
1. Add polling or WebSocket for session changes
2. Animate workers when they're actively working (tool calls)
3. Flash/pulse when attention state changes

## What to Keep from game-ui

- `ForumCanvas.tsx` - rendering engine (pan/zoom/selection)
- `layout.ts` - isometric transforms
- `types.ts` - base types (ForumEntity, ForumMarker, etc.)
- `state.ts` - state management with Maps

## What to Remove

- `replay.ts` - fake data generator
- `useForumReplay.ts` - replay playback hook
- Most of `live-mapper.ts` - SSE event mapping (may repurpose)

## API Needs

New or extended endpoints:

```
GET /api/jarvis/life-hub/sessions
  ?active=true           # only sessions with ended_at IS NULL or recent
  &include_attention=true # include attention classification
  &limit=50

Response:
{
  sessions: [{
    id, project, provider, cwd,
    status: 'working' | 'thinking' | 'waiting' | 'idle' | 'completed' | 'error',
    attention: 'hard' | 'needs' | 'soft' | 'auto',
    last_activity: timestamp,
    last_message: string (truncated)
  }]
}
```

## Success Criteria

1. Open `/forum`, see your 5 Claude Code sessions as workers
2. Workers grouped by project (zerg room, life-hub room, etc.)
3. Glowing red aura on workers that need attention
4. Click worker → see what they're working on
5. Know at a glance which terminal tab to switch to
