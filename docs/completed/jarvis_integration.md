# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---

# Jarvis Integration Architecture

> **⚠️ DEPRECATED**: This document describes the original architecture.
> As of December 2024, jarvis-server has been removed and all functionality moved to backend at `/api/jarvis/*`.
> See `AGENTS.md` for current architecture.

**Version**: 1.0
**Date**: October 6, 2025
**Status**: Backend Complete, UI Integration Pending

## Overview

The Swarm Platform integrates **Jarvis** (voice/text UI) with **Zerg** (agent orchestration backend) to provide a unified AI assistant experience. Jarvis serves as the human interface while Zerg handles all scheduling, workflows, and tool execution.

## System Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                         Jarvis PWA                            │
│  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐ │
│  │  Voice Input   │  │   Text Input   │  │   Task Inbox   │ │
│  │  (OpenAI RT)   │  │   (Keyboard)   │  │   (SSE Feed)   │ │
│  └────────────────┘  └────────────────┘  └────────────────┘ │
│           │                  │                      │         │
│           └──────────────────┴──────────────────────┘         │
│                              │                                │
│                    ┌─────────▼─────────┐                     │
│                    │  Jarvis API Client │                     │
│                    │  (JWT Auth)        │                     │
│                    └─────────┬─────────┘                     │
└──────────────────────────────┼──────────────────────────────┘
                               │ HTTPS
                    ┌──────────▼──────────┐
                    │   /api/jarvis/*     │
                    │   (FastAPI Router)   │
                    └──────────┬──────────┘
                               │
      ┌────────────────────────┼────────────────────────┐
      │                        │                        │
┌─────▼──────┐      ┌─────────▼────────┐      ┌───────▼────────┐
│  Auth      │      │  Agent/Run CRUD   │      │  SSE Stream    │
│  (Device   │      │  (Database)       │      │  (Event Bus)   │
│   Secret)  │      └──────────┬────────┘      └───────┬────────┘
└────────────┘                 │                       │
                    ┌──────────▼────────┐             │
                    │  Task Runner      │             │
                    │  (execute_agent)  │             │
                    └──────────┬────────┘             │
                               │                      │
                    ┌──────────▼──────────┐           │
                    │   LangGraph         │           │
                    │   Workflows         │           │
                    └──────────┬──────────┘           │
                               │                      │
                    ┌──────────▼──────────┐           │
                    │   Event Bus         │───────────┘
                    │   (run_updated)     │
                    └─────────────────────┘
```

## API Endpoints

### Authentication

#### POST /api/jarvis/auth

Authenticate Jarvis device and establish a session.

**Request**:

```json
{
  "device_secret": "your-secret-from-env"
}
```

**Response**:

```json
{
  "session_cookie_name": "jarvis_session",
  "session_expires_in": 604800
}
```

**Flow**:

1. Validates device secret against `JARVIS_DEVICE_SECRET` env var
2. Creates or fetches `jarvis@swarm.local` ADMIN user
3. Issues JWT token (7-day expiry) and sets HttpOnly cookie (`jarvis_session`)
4. Jarvis stores expiry metadata locally for re-auth reminders

**Errors**:

- `401 Unauthorized`: Invalid device secret
- `500 Internal Server Error`: JARVIS_DEVICE_SECRET not configured

### Agent Management

#### GET /api/jarvis/agents

List all available agents with their schedules.

**Authentication**:

- HttpOnly session cookie set during `/api/jarvis/auth`
- Development override: when `AUTH_DISABLED=1`, standard dev auth applies

**Response**:

```json
[
  {
    "id": 1,
    "name": "Morning Digest",
    "status": "idle",
    "schedule": "0 7 * * *",
    "next_run_at": "2025-10-07T07:00:00Z",
    "description": "You are the Morning Digest assistant..."
  }
]
```

**Use Case**: Populate Jarvis UI with available agents for voice/text commands like "Run my morning digest"

### Run History

#### GET /api/jarvis/runs

Get recent agent execution history.

**Authentication**:

- HttpOnly session cookie set by `/api/jarvis/auth`
- Development override: when `AUTH_DISABLED=1`, standard dev auth applies

**Query Parameters**:

- `limit` (optional, default 50): Maximum number of runs to return
- `agent_id` (optional): Filter by specific agent

**Response**:

```json
[
  {
    "id": 42,
    "agent_id": 1,
    "agent_name": "Morning Digest",
    "status": "success",
    "summary": "Recovery: 78%, Sleep: 7h 23m, 3 meetings today...",
    "created_at": "2025-10-06T07:00:15Z",
    "updated_at": "2025-10-06T07:00:42Z",
    "completed_at": "2025-10-06T07:00:42Z"
  }
]
```

**Use Case**: Display in Jarvis Task Inbox, sorted by most recent first

### Task Dispatch

#### POST /api/jarvis/dispatch

Trigger immediate agent execution from Jarvis.

**Authentication**:

- HttpOnly session cookie set by `/api/jarvis/auth`
- Development override: when `AUTH_DISABLED=1`, standard dev auth applies

**Headers**:

```
Content-Type: application/json
```

**Request**:

```json
{
  "agent_id": 1,
  "task_override": "Focus on sleep quality from last night only"
}
```

**Response**:

```json
{
  "run_id": 43,
  "thread_id": 87,
  "status": "queued",
  "agent_name": "Health Watch"
}
```

**Errors**:

- `404 Not Found`: Agent doesn't exist
- `409 Conflict`: Agent already running
- `500 Internal Server Error`: Execution failure

**Use Case**: User says "Check my health stats" → Jarvis dispatches Health Watch agent

### Real-Time Events

#### GET /api/jarvis/events

Server-Sent Events stream for real-time updates.

**Authentication**:

- HttpOnly session cookie set by `/api/jarvis/auth`
- Development override: when `AUTH_DISABLED=1`, standard dev auth applies

**Event Types**:

1. **connected** - Initial connection

```
event: connected
data: {"message": "Jarvis SSE stream connected"}
```

2. **heartbeat** - Keep-alive (every 30s)

```
event: heartbeat
data: {"timestamp": "2025-10-06T19:00:00Z"}
```

3. **run_created** - New agent run started

```
event: run_created
data: {
  "type": "run_created",
  "payload": {
    "run_id": 43,
    "agent_id": 1,
    "status": "queued"
  },
  "timestamp": "2025-10-06T19:00:15Z"
}
```

4. **run_updated** - Run status changed

```
event: run_updated
data: {
  "type": "run_updated",
  "payload": {
    "run_id": 43,
    "status": "success",
    "summary": "Recovery: 78%..."
  },
  "timestamp": "2025-10-06T19:00:42Z"
}
```

5. **agent_updated** - Agent config/status changed

```
event: agent_updated
data: {
  "type": "agent_updated",
  "payload": {
    "agent_id": 1,
    "status": "idle"
  },
  "timestamp": "2025-10-06T19:00:45Z"
}
```

**Use Case**:

- Jarvis opens SSE connection on startup
- Receives real-time updates for Task Inbox
- Can speak notifications when runs complete
- Updates UI without polling

## Integration Flow

### Startup Sequence

```
1. Jarvis PWA loads
   ↓
2. Check localStorage for valid JWT
   ↓
3. If no token OR expired:
   → POST /api/jarvis/auth with JARVIS_DEVICE_SECRET
   → Store token in localStorage
   ↓
4. GET /api/jarvis/agents
   → Cache agent list for voice/text dispatch
   ↓
5. GET /api/jarvis/events
   → Open SSE connection for real-time updates
   ↓
6. GET /api/jarvis/runs (limit=20)
   → Display recent history in Task Inbox
   ↓
7. Ready for user interaction
```

### Voice Command Flow

```
User: "Run my morning digest"
   ↓
1. Jarvis speech recognition → text
   ↓
2. Parse intent → agent_id lookup
   ↓
3. POST /api/jarvis/dispatch { agent_id: 1 }
   ↓
4. Receive: { run_id: 43, thread_id: 87, status: "queued" }
   ↓
5. SSE stream sends run_created event
   → Update Task Inbox: "Morning Digest - Running..."
   ↓
6. SSE stream sends run_updated (status: success, summary: "...")
   → Update Task Inbox: "Morning Digest - Complete ✓"
   → Optionally speak summary via TTS
   ↓
7. User sees/hears result
```

### Scheduled Agent Flow

```
1. APScheduler triggers at 7:00 AM
   ↓
2. SchedulerService calls execute_agent_task(agent_id=1)
   ↓
3. Task runner creates AgentRun and Thread
   ↓
4. Event bus publishes run_created
   ↓
5. Jarvis SSE stream receives event
   → Task Inbox shows: "Morning Digest - Running..."
   ↓
6. LangGraph executes workflow
   ↓
7. Event bus publishes run_updated (success, summary)
   ↓
8. Jarvis SSE stream receives event
   → Task Inbox shows: "Morning Digest - Complete ✓"
   → Optionally push notification if PWA is backgrounded
```

## Data Models

### JarvisAuthRequest

```typescript
{
  device_secret: string;
}
```

### JarvisAuthResponse

```typescript
{
  session_cookie_name: string; // "jarvis_session"
  session_expires_in: number; // Seconds (604800 = 7 days)
}
```

### JarvisAgentSummary

```typescript
{
  id: number;
  name: string;
  status: "idle" | "running";
  schedule?: string;       // Cron expression
  next_run_at?: string;    // ISO timestamp
  description?: string;    // Truncated system instructions
}
```

### JarvisRunSummary

```typescript
{
  id: number;
  agent_id: number;
  agent_name: string;
  status: "queued" | "running" | "success" | "failed";
  summary?: string;        // Brief summary for display
  created_at: string;      // ISO timestamp
  updated_at: string;
  completed_at?: string;
}
```

### JarvisDispatchRequest

```typescript
{
  agent_id: number;
  task_override?: string;  // Optional custom instructions
}
```

### JarvisDispatchResponse

```typescript
{
  run_id: number;
  thread_id: number;
  status: string;
  agent_name: string;
}
```

## Security Model

### Device Authentication

- Jarvis uses a pre-shared **device secret** stored in environment
- Secret is exchanged for a JWT token with 7-day expiry
- Token includes `jarvis@swarm.local` user identity with ADMIN role
- Token stored in localStorage (IndexedDB in production)

### Token Lifecycle

```
Day 0: Authenticate with device secret → Token (7 day expiry)
Day 1-6: Use stored token for all API calls
Day 7: Token expires → Re-authenticate automatically
```

### Authorization

- All `/api/jarvis/*` endpoints require the HttpOnly `jarvis_session` cookie issued by `/api/jarvis/auth`
- Cookie is validated via `get_current_jarvis_user`
- Dev mode (`AUTH_DISABLED=1`) still honors the global dev auth fallback
- Multi-device support: each device authenticates to obtain its own session cookie

## Environment Configuration

### Development Setup Options

#### Option A: Unified Docker Compose (Recommended)

All services run in a single Docker network with Nginx reverse proxy.

**Zerg Backend (.env)**:

```bash
# Required ports (fail-fast configuration - must be set)
JARPXY_PORT=30080         # External: Jarvis PWA entry point
ZGPXY_PORT=30081          # External: Zerg Dashboard entry point
JARVIS_WEB_PORT=8080      # Internal: jarvis-web container
JARVIS_SERVER_PORT=8787   # Legacy: jarvis-server (removed; backend now serves /api/jarvis/*)
ZERG_BACKEND_PORT=8000    # Internal: backend container
ZERG_FRONTEND_PORT=5173   # Internal: frontend-web container

# Existing Zerg vars
JARVIS_DEVICE_SECRET="your-secure-secret-change-me-min-32-chars"
OPENAI_API_KEY="sk-..."
DATABASE_URL="sqlite:///./app.db"
JWT_SECRET="your-jwt-secret"
```

**Jarvis Client**:

```bash
# Uses proxy path (no hardcoded hostnames)
VITE_ZERG_API_URL="/api/zerg"
JARVIS_DEVICE_SECRET="your-secure-secret-change-me-min-32-chars"
OPENAI_API_KEY="sk-..."
```

**Access URLs**:

- Jarvis PWA: http://localhost:30080
- Zerg Dashboard: http://localhost:30081

#### Option B: Traditional Docker Compose

Separate services with direct connections.

**Zerg Backend (.env)**:

```bash
# Jarvis Integration
JARVIS_DEVICE_SECRET="your-secure-secret-change-me-min-32-chars"

# Existing Zerg vars
BACKEND_PORT=47300
OPENAI_API_KEY="sk-..."
DATABASE_URL="sqlite:///./app.db"
JWT_SECRET="your-jwt-secret"
```

**Jarvis Client** _(was `apps/zerg/frontend-web/src/jarvis/.env`, now integrated at `apps/zerg/frontend-web/`)_:

```bash
# Direct connection to Zerg backend
VITE_ZERG_API_URL="http://localhost:47300"
JARVIS_DEVICE_SECRET="your-secure-secret-change-me-min-32-chars"

# OpenAI for local voice processing
OPENAI_API_KEY="sk-..."
```

**Access URLs**:

- Jarvis PWA: http://localhost:8080
- Zerg Backend: http://localhost:47300
- Zerg Frontend: http://localhost:47200

## Implementation Status

### ✅ Complete (Backend)

- [x] Device secret authentication
- [x] JWT token issuance (7-day expiry)
- [x] Agent listing endpoint
- [x] Run history endpoint
- [x] Dispatch endpoint with task execution
- [x] SSE event streaming
- [x] Event bus integration
- [x] AgentRun.summary column
- [x] Alembic migration
- [x] TypeScript API client
- [x] Seed script for baseline agents
- [x] Tool manifest generation

### 🚧 Pending (Frontend)

- [ ] Task Inbox UI component
- [ ] Text input mode in Jarvis PWA
- [ ] SSE connection management
- [ ] Voice notification on run complete
- [ ] PWA manifest updates
- [ ] Agent dispatch from voice commands
- [ ] Run summary display in UI

### 📋 Future Enhancements

- [ ] Web push notifications (background PWA)
- [ ] Offline dispatch queue
- [ ] Multi-device sync
- [ ] Run evaluation harness
- [ ] Visual workflow builder
- [ ] Tool manifest sync validation

## Developer Guide

### Testing the Backend

#### Option A: Unified Docker Compose

```bash
# Start all services
cd /Users/davidrose/git/zerg
make dev

# Wait for containers to be healthy (10-15 seconds)
docker compose --project-name zerg --env-file .env -f docker/docker-compose.dev.yml --profile full ps

# Test endpoints (store cookie jar)
COOKIE_JAR=cookies.txt
curl -s -X POST http://localhost:30080/api/session \
  -H "Content-Type: application/json" \
  -d '{"device_secret":"your-secret"}' \
  -c "$COOKIE_JAR" -b "$COOKIE_JAR"

# Note: Unified mode uses /api/jarvis/session (served by backend; jarvis-server removed)
# All other /api/* routes are proxied to backend
```

#### Option B: Traditional Setup

```bash
# Start Zerg only (direct ports)
cd /Users/davidrose/git/zerg
make zerg

# In another terminal, test endpoints (store cookie jar)
COOKIE_JAR=cookies.txt
curl -s -X POST http://localhost:47300/api/jarvis/auth \
  -H "Content-Type: application/json" \
  -d '{"device_secret":"your-secret"}' \
  -c "$COOKIE_JAR" -b "$COOKIE_JAR"

# List agents
curl http://localhost:47300/api/jarvis/agents \
  -b "$COOKIE_JAR"

# Dispatch agent
curl -X POST http://localhost:47300/api/jarvis/dispatch \
  -H "Content-Type: application/json" \
  -d '{"agent_id":1}' \
  -b "$COOKIE_JAR"

# Listen to SSE stream
curl -N http://localhost:47300/api/jarvis/events \
  -b "$COOKIE_JAR"
```

**Note**: In unified mode, API routes are split between:

- Legacy: `/api/session`, `/api/tool`, `/api/sync/*` → jarvis-server (Port 8787) (removed)
- `/api/zerg/*` → backend (Port 8000)

### Seeding Baseline Agents

#### Option A: Unified Docker Compose

```bash
cd /Users/davidrose/git/zerg
make dev

# Wait for backend to be healthy, then seed:
BACKEND=$(docker ps --format "{{.Names}}" | rg "backend" | head -n 1)
docker exec "$BACKEND" uv run python scripts/seed_jarvis_agents.py
```

#### Option B: Traditional Setup

```bash
cd /Users/davidrose/git/zerg
make seed-jarvis-agents

# Or directly:
cd apps/zerg/backend
uv run python scripts/seed_jarvis_agents.py
```

This creates 4 baseline agents:

1. **Morning Digest** - 7 AM daily (health + calendar + weather)
2. **Health Watch** - 8 PM daily (WHOOP trends and insights)
3. **Weekly Planning** - 6 PM Sundays (week ahead planning)
4. **Quick Status** - On-demand (time, weather, next event)

### Integrating in Jarvis PWA

```typescript
import { getJarvisClient } from "@jarvis/core";

// Initialize client
const client = getJarvisClient(import.meta.env.VITE_ZERG_API_URL);

// Authenticate on startup
await client.authenticate(import.meta.env.JARVIS_DEVICE_SECRET);

// List agents
const agents = await client.listAgents();

// Dispatch agent
const result = await client.dispatch({ agent_id: 1 });

// Connect to SSE stream
client.connectEventStream({
  onConnected: () => console.log("Connected to Zerg"),
  onRunCreated: (event) => updateTaskInbox(event),
  onRunUpdated: (event) => {
    updateTaskInbox(event);
    if (event.payload.status === "success") {
      speakResult(event.payload.summary);
    }
  },
  onError: (error) => console.error("SSE error:", error),
});
```

## Database Schema

### AgentRun Extensions

```sql
-- New column added in migration a1b2c3d4e5f6
ALTER TABLE agent_runs ADD COLUMN summary TEXT NULL;

-- Updated by crud.mark_finished() when run completes
-- Contains first assistant response or truncated output (max 500 chars)
```

### Jarvis Service User

```sql
-- Auto-created on first /auth call
INSERT INTO users (email, provider, role, display_name)
VALUES ('jarvis@swarm.local', 'jarvis', 'ADMIN', 'Jarvis Assistant');
```

## Event Bus Protocol

### Event Structure

All events published to the event bus follow this structure:

```python
{
    "event_type": "run_created" | "run_updated" | "agent_updated",
    "run_id": int,
    "agent_id": int,
    "status": str,
    "summary": str | None,
    "timestamp": str,  # ISO format
}
```

### Subscriptions

The SSE endpoint subscribes to:

- `EventType.RUN_CREATED` - New runs initiated
- `EventType.RUN_UPDATED` - Status changes, summaries added
- `EventType.AGENT_UPDATED` - Agent config/status changes

## Error Handling

### Client-Side Retries

```typescript
// Authentication retry
async function authenticateWithRetry(maxAttempts = 3) {
  for (let i = 0; i < maxAttempts; i++) {
    try {
      return await client.authenticate(deviceSecret);
    } catch (e) {
      if (i === maxAttempts - 1) throw e;
      await sleep(1000 * Math.pow(2, i)); // Exponential backoff
    }
  }
}

// SSE reconnection
client.connectEventStream({
  onError: () => {
    setTimeout(() => client.connectEventStream(...), 5000);
  },
});
```

### Backend Error Codes

| Code | Meaning               | Action              |
| ---- | --------------------- | ------------------- |
| 401  | Invalid token         | Re-authenticate     |
| 404  | Agent not found       | Update agent list   |
| 409  | Agent already running | Wait for completion |
| 500  | Server error          | Retry with backoff  |

## Performance Considerations

### SSE Connection Management

- **Heartbeat**: 30-second intervals prevent timeout
- **Queue depth**: 100 events max (drops oldest)
- **Reconnection**: Client should auto-reconnect on error
- **Connection limit**: 1 per Jarvis instance

### API Rate Limits

- **Auth**: No limit (device secret validation only)
- **Agents/Runs**: No limit (read-only)
- **Dispatch**: Limited by agent locking (1 concurrent run per agent)

### Caching Strategy

- **Agent list**: Cache for 60 seconds in Jarvis
- **Token**: Cache until 1 hour before expiry
- **Run summaries**: Fetch on SSE events, not polling

## Troubleshooting

### "Not authenticated" errors

1. Check `JARVIS_DEVICE_SECRET` matches in both .env files
2. Verify token in localStorage hasn't expired
3. Check backend logs for auth failures

### SSE stream not receiving events

1. Verify event_bus subscribers are active
2. Check backend logs for "Jarvis SSE stream connected"
3. Ensure runs are actually completing (check DB)
4. Test with curl to isolate client vs server issues

### Agent dispatch fails with 409

- Agent is already running
- Check `/api/jarvis/runs` for active run
- Wait for completion or cancel via Zerg UI

### Missing summaries in run history

- `AgentRun.summary` column might not be populated yet
- Check `crud.mark_finished()` populates summary field
- Verify migration ran: `uv run alembic current`

## Next Steps

### Immediate (Jarvis UI)

1. Implement Task Inbox component
2. Add text input mode
3. Connect SSE stream on startup
4. Display agent list and run history
5. Voice command → dispatch integration

### Near-Term (Features)

1. Push notifications for backgrounded PWA
2. Offline dispatch queue
3. Run detail view (full conversation)
4. Agent scheduling UI
5. Tool manifest validation

### Long-Term (Platform)

1. Multi-model support (Anthropic, local LLMs)
2. Visual workflow builder in Jarvis
3. Evaluation harness and metrics
4. Analytics dashboard
5. Native mobile apps (Capacitor)

## References

- **Blueprint**: `/swarm_platform_blueprint.md` _(archived/removed)_
- **Progress Report**: `/JARVIS_INTEGRATION_PROGRESS.md` _(archived/removed)_
- **Backend Router**: `/apps/zerg/backend/zerg/routers/jarvis.py` _(still valid)_
- **TypeScript Client**: `/apps/zerg/frontend-web/src/jarvis/core/jarvis-api-client.ts` _(now: `apps/zerg/frontend-web/src/jarvis/core/jarvis-api-client.ts`)_
- **Seed Script**: `/apps/zerg/backend/scripts/seed_jarvis_agents.py` _(still valid)_
- **Migration**: `/apps/zerg/backend/alembic/versions/a1b2c3d4e5f6_add_summary_to_agent_run.py` _(example path)_
