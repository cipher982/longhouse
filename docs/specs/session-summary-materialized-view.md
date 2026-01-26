# Session Summary Materialized View

## Overview

Add a materialized view to Life Hub's `agents` schema that pre-computes session state, attention level, and recent activity. This enables fast queries for the Forum UI without expensive joins on every request.

## Schema

```sql
-- Life Hub database, agents schema
CREATE MATERIALIZED VIEW agents.session_summary AS
WITH last_events AS (
  -- Get the most recent event per session
  SELECT DISTINCT ON (session_id)
    session_id,
    timestamp as last_event_at,
    role as last_event_role,
    tool_name as last_event_tool
  FROM agents.events
  ORDER BY session_id, timestamp DESC
),
last_messages AS (
  -- Get last user and assistant messages per session
  SELECT DISTINCT ON (session_id, role)
    session_id,
    role,
    LEFT(content_text, 300) as content_preview,
    timestamp,
    tool_name
  FROM agents.events
  WHERE role IN ('user', 'assistant')
    AND content_text IS NOT NULL
    AND content_text != ''
  ORDER BY session_id, role, timestamp DESC
),
attention_signals AS (
  -- Check for AskUserQuestion in recent events
  SELECT DISTINCT session_id,
    TRUE as has_ask_user_question
  FROM agents.events
  WHERE tool_name = 'AskUserQuestion'
    AND timestamp > NOW() - INTERVAL '24 hours'
)
SELECT
  s.id,
  s.project,
  s.provider,
  s.cwd,
  s.git_repo,
  s.git_branch,
  s.device_id,
  s.started_at,
  s.ended_at,
  s.user_messages,
  s.assistant_messages,
  s.tool_calls,

  -- Computed: last activity
  COALESCE(le.last_event_at, s.started_at) as last_activity_at,

  -- Computed: session status
  CASE
    WHEN s.ended_at IS NOT NULL THEN 'completed'
    WHEN le.last_event_at < NOW() - INTERVAL '30 minutes' THEN 'idle'
    WHEN le.last_event_tool IS NOT NULL THEN 'working'
    WHEN le.last_event_role = 'assistant' THEN 'thinking'
    ELSE 'active'
  END as status,

  -- Computed: attention level
  CASE
    -- Hard: errors or explicit failures
    WHEN s.ended_at IS NOT NULL
      AND EXISTS (
        SELECT 1 FROM agents.events e
        WHERE e.session_id = s.id
          AND e.content_text ILIKE '%error%'
          AND e.timestamp > s.ended_at - INTERVAL '1 minute'
      ) THEN 'hard'
    -- Needs: AskUserQuestion was called
    WHEN asig.has_ask_user_question THEN 'needs'
    -- Soft: last assistant message ends with question
    WHEN lm_asst.content_preview LIKE '%?' THEN 'soft'
    -- Soft: idle for a while but not ended
    WHEN s.ended_at IS NULL
      AND le.last_event_at < NOW() - INTERVAL '10 minutes' THEN 'soft'
    -- Auto: everything else
    ELSE 'auto'
  END as attention,

  -- Computed: duration
  EXTRACT(EPOCH FROM (COALESCE(s.ended_at, NOW()) - s.started_at)) / 60 as duration_minutes,

  -- Last messages for preview
  lm_user.content_preview as last_user_message,
  lm_user.timestamp as last_user_message_at,
  lm_asst.content_preview as last_assistant_message,
  lm_asst.timestamp as last_assistant_message_at,

  -- Metadata for refresh tracking
  NOW() as refreshed_at

FROM agents.sessions s
LEFT JOIN last_events le ON le.session_id = s.id
LEFT JOIN last_messages lm_user ON lm_user.session_id = s.id AND lm_user.role = 'user'
LEFT JOIN last_messages lm_asst ON lm_asst.session_id = s.id AND lm_asst.role = 'assistant'
LEFT JOIN attention_signals asig ON asig.session_id = s.id
WHERE s.started_at > NOW() - INTERVAL '7 days';  -- Only recent sessions

-- Index for common queries
CREATE UNIQUE INDEX ON agents.session_summary (id);
CREATE INDEX ON agents.session_summary (project);
CREATE INDEX ON agents.session_summary (attention);
CREATE INDEX ON agents.session_summary (status);
CREATE INDEX ON agents.session_summary (last_activity_at DESC);
```

## Refresh Strategy

### Option A: Scheduled refresh (simplest)
```sql
-- Cron job every 30 seconds
REFRESH MATERIALIZED VIEW CONCURRENTLY agents.session_summary;
```

Requires the `UNIQUE INDEX` on `id` for `CONCURRENTLY` (non-blocking refresh).

### Option B: On-demand refresh
```python
# Life Hub endpoint
@router.post("/agents/refresh-summary")
async def refresh_session_summary(db: Session):
    db.execute(text("REFRESH MATERIALIZED VIEW CONCURRENTLY agents.session_summary"))
    db.commit()
    return {"status": "refreshed", "timestamp": datetime.utcnow()}
```

Call from Zerg before serving Forum UI, or on a timer.

### Option C: Trigger-based (complex)
Refresh on INSERT to agents.events. Not recommended - too frequent.

**Recommendation**: Option A (scheduled) with Option B available for manual refresh.

## Zerg API Changes

### New endpoint: GET /api/jarvis/life-hub/sessions/active

```python
@router.get("/sessions/active")
async def get_active_sessions(
    project: Optional[str] = None,
    attention: Optional[str] = None,  # 'hard', 'needs', 'soft', 'auto'
    status: Optional[str] = None,     # 'working', 'thinking', 'idle', 'completed'
    limit: int = Query(50, le=100),
    db: Session = Depends(get_db),
) -> ActiveSessionsResponse:
    """Get active sessions from the materialized view."""

    query = """
        SELECT
            id, project, provider, cwd, git_branch,
            started_at, ended_at, last_activity_at,
            status, attention, duration_minutes,
            last_user_message, last_assistant_message,
            user_messages, assistant_messages, tool_calls,
            refreshed_at
        FROM agents.session_summary
        WHERE 1=1
    """
    params = {"limit": limit}

    if project:
        query += " AND project = :project"
        params["project"] = project

    if attention:
        query += " AND attention = :attention"
        params["attention"] = attention

    if status:
        query += " AND status = :status"
        params["status"] = status

    query += " ORDER BY last_activity_at DESC LIMIT :limit"

    result = db.execute(text(query), params)
    # ... map to response
```

### Response model

```python
class ActiveSession(BaseModel):
    id: str
    project: Optional[str]
    provider: str
    cwd: Optional[str]
    git_branch: Optional[str]
    started_at: datetime
    ended_at: Optional[datetime]
    last_activity_at: datetime
    status: Literal['working', 'thinking', 'idle', 'completed', 'active']
    attention: Literal['hard', 'needs', 'soft', 'auto']
    duration_minutes: float
    last_user_message: Optional[str]
    last_assistant_message: Optional[str]
    message_count: int
    tool_calls: int
    refreshed_at: datetime

class ActiveSessionsResponse(BaseModel):
    sessions: List[ActiveSession]
    total: int
    last_refresh: datetime
```

## Forum UI Integration

### Hook: useActiveSessions

```typescript
function useActiveSessions(options?: {
  project?: string;
  attention?: string;
  pollInterval?: number;  // default 10000ms
}) {
  return useQuery({
    queryKey: ['active-sessions', options],
    queryFn: () => request<ActiveSessionsResponse>('/jarvis/life-hub/sessions/active', {
      params: options
    }),
    refetchInterval: options?.pollInterval ?? 10000,
  });
}
```

### ForumPage changes

```typescript
// Replace fake replay data with real sessions
const { data: sessionsData } = useActiveSessions({ pollInterval: 5000 });

// Map sessions to canvas entities
const entities = useMemo(() => {
  if (!sessionsData?.sessions) return new Map();

  return new Map(sessionsData.sessions.map((session, index) => {
    const entity: ForumEntity = {
      id: session.id,
      type: 'worker',
      label: session.project || 'unknown',
      status: mapStatusToEntityStatus(session.status),
      roomId: session.project || 'default',
      position: computePosition(session.project, index),
      // Visual styling based on provider
      variant: session.provider,  // 'claude', 'codex', 'gemini'
    };
    return [session.id, entity];
  }));
}, [sessionsData]);

// Attention indicators as markers
const markers = useMemo(() => {
  if (!sessionsData?.sessions) return new Map();

  return new Map(sessionsData.sessions
    .filter(s => s.attention === 'hard' || s.attention === 'needs')
    .map(session => {
      const marker: ForumMarker = {
        id: `attention-${session.id}`,
        type: session.attention === 'hard' ? 'alert' : 'focus',
        roomId: session.project || 'default',
        position: computePosition(session.project, 0),
        label: session.attention === 'hard' ? '!' : '?',
        createdAt: Date.now(),
        expiresAt: Date.now() + 60000,
      };
      return [marker.id, marker];
    }));
}, [sessionsData]);
```

## Migration Plan

### Step 1: Create materialized view in Life Hub
```bash
# Run migration in life-hub repo
psql $DATABASE_URL -f migrations/add_session_summary_view.sql
```

### Step 2: Set up refresh schedule
```bash
# Add to Life Hub's scheduler or use pg_cron
SELECT cron.schedule('refresh-session-summary', '*/30 * * * * *',
  'REFRESH MATERIALIZED VIEW CONCURRENTLY agents.session_summary');
```

### Step 3: Add Zerg API endpoint
- Add `GET /api/jarvis/life-hub/sessions/active`
- Uses the materialized view

### Step 4: Update Forum UI
- Add `useActiveSessions` hook
- Replace replay data with real sessions in live mode
- Keep replay mode for demos

## Performance Expectations

| Query | Without MV | With MV |
|-------|-----------|---------|
| List 50 active sessions | ~500ms | ~10ms |
| Filter by project | ~300ms | ~5ms |
| Filter by attention | ~400ms | ~5ms |

Materialized view size estimate: ~100KB for 1000 sessions.

Refresh time: ~1-2 seconds for CONCURRENT refresh.

## Future Enhancements

1. **Real-time refresh trigger**: Refresh when new events arrive (debounced)
2. **Per-user filtering**: Add user_id to sessions, filter in view
3. **Attention history**: Track attention state changes over time
4. **Session relationships**: Link continuations, show session chains
