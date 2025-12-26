# ⚠️ ARCHIVED / HISTORICAL REFERENCE ONLY

> **Note:** Paths and implementation details in this document may be outdated.
> For current information, refer to [AGENTS.md](../../AGENTS.md) or the root `docs/README.md`.

---

# E2E Test Infrastructure Bugs

**Date**: 2025-12-17
**Context**: Discovered while attempting to run Playwright E2E tests for the new two-phase supervisor progress indicator.

## Executive Summary

The Jarvis E2E test infrastructure (`apps/zerg/frontend-web/src/jarvis/docker-compose.test.yml`) is currently broken. Tests cannot run because:

1. Database tables don't get created on fresh test DB
2. Chat session never connects (input stays disabled)
3. SSE event subscribers are not being registered

All 10 new progress indicator tests fail, as do existing tests like `text-message-happy-path.e2e.spec.ts`.

---

## Bug 1: Database Migration is a No-Op

### Symptom

```
sqlalchemy.exc.ProgrammingError: relation "agents" does not exist
[SQL: ALTER TABLE agents ADD CONSTRAINT uq_agent_owner_name UNIQUE (owner_id, name)]
```

### Root Cause

The initial migration (`458f9a6a8779_initial_schema_baseline.py`) is a **no-op**:

```python
def upgrade() -> None:
    """Initial schema baseline - no-op since tables already exist."""
    # This is a baseline migration for existing databases.
    # Tables were created via SQLAlchemy create_all() or admin reset.
    pass
```

This assumes tables already exist (created via `create_all()` or admin reset), but in a fresh Docker test environment, they don't.

### Impact

- Backend starts with no tables
- All database operations fail
- Session creation fails
- User operations fail

### Workaround Found

```bash
docker compose -f apps/zerg/frontend-web/src/jarvis/docker-compose.test.yml exec -T backend python -c "
from zerg.database import initialize_database
initialize_database()
"
```

### Proper Fix Needed

Either:

1. Change initial migration to actually create tables (not assume they exist)
2. Add a pre-migration step to `start.sh` that calls `initialize_database()` if tables don't exist
3. Create a proper "from scratch" migration that creates the full schema

---

## Bug 2: Chat Session Never Connects

### Symptom

Tests time out waiting for input to be enabled:

```
textbox "Message input" [disabled]
```

Page shows "System Ready" and "Tap the microphone or type a message to begin" but the text input never becomes enabled.

### Evidence from Page Snapshot

```yaml
- textbox "Message input" [disabled] [ref=e51]:
    - /placeholder: Type a message...
- button "Send message" [disabled] [ref=e52]
```

### Root Cause

The Jarvis frontend waits for a session to be established before enabling input. The session establishment is failing silently.

Possible causes:

1. API endpoint `/api/jarvis/session` failing
2. SSE connection not establishing
3. Auth/cookie issues in test environment
4. Frontend not receiving expected response format

### Impact

- Cannot send any messages
- All E2E tests fail at the "wait for input enabled" step
- 90-second timeout on every test

### Investigation Needed

1. Check browser console logs for errors
2. Check network tab for failed API calls
3. Verify `/api/jarvis/session` endpoint works
4. Check if cookies are being set properly

---

## Bug 3: SSE Event Subscribers = 0

### Symptom

Backend logs show events firing but no subscribers:

```
🔥🔥🔥 EVENT_BUS.publish(EventType.SUPERVISOR_STARTED): 0 subscribers
❌ NO SUBSCRIBERS for EventType.SUPERVISOR_STARTED
🔥🔥🔥 EVENT_BUS.publish(EventType.SUPERVISOR_THINKING): 0 subscribers
❌ NO SUBSCRIBERS for EventType.SUPERVISOR_THINKING
🔥🔥🔥 EVENT_BUS.publish(EventType.SUPERVISOR_COMPLETE): 0 subscribers
❌ NO SUBSCRIBERS for EventType.SUPERVISOR_COMPLETE
```

### Root Cause

The SSE stream endpoint (`/api/jarvis/chat`) subscribes to these events when a client connects. Since no client is successfully connecting (Bug 2), there are no subscribers.

This is a symptom of Bug 2, not a separate bug.

### Impact

- Even if messages could be sent, progress events wouldn't reach the frontend
- Progress indicator would never show

---

## Bug 4: LangGraph Checkpointer Auth Failure

### Symptom

```
ERROR Failed to setup PostgresSaver tables: connection failed:
connection to server at "172.19.0.2", port 5432 failed:
FATAL: password authentication failed for user "test_user"
WARNING Falling back to MemorySaver due to setup failure
```

### Root Cause

The LangGraph `PostgresSaver` is trying to connect with credentials that don't match. The main app connects fine, but the checkpointer uses different connection logic.

### Impact

- Non-fatal: Falls back to `MemorySaver`
- Conversation state won't persist across restarts
- May cause issues with multi-turn conversations in tests

### Fix Needed

Ensure LangGraph checkpointer uses the same `DATABASE_URL` as the main application.

---

## Bug 5: Docker Image Not Rebuilding with Code Changes

### Symptom

Code changes to `supervisor-progress.ts` may not be reflected in tests.

### Root Cause

The `docker-compose.test.yml` mounts source files as volumes:

```yaml
volumes:
  - ../../apps/zerg/frontend-web/src/jarvis/src:/app/apps/web/src:ro
  - ../../apps/zerg/frontend-web/src/jarvis/lib:/app/apps/web/lib:ro
```

However, if the built assets inside the container don't include the new code (because the image was built before the changes), the mounts won't help for compiled output.

### Fix Needed

Either:

1. Rebuild images after code changes: `docker compose build --no-cache`
2. Ensure dev server in container watches mounted files and rebuilds
3. Mount the entire source and run build inside container

---

## Reproduction Steps

```bash
cd /Users/davidrose/git/zerg

# Start test environment
docker compose -f apps/zerg/frontend-web/src/jarvis/docker-compose.test.yml up -d

# Wait for healthy
docker compose -f apps/zerg/frontend-web/src/jarvis/docker-compose.test.yml ps

# Check backend logs - will show migration failures
docker compose -f apps/zerg/frontend-web/src/jarvis/docker-compose.test.yml logs backend

# Run tests - all will fail
docker compose -f apps/zerg/frontend-web/src/jarvis/docker-compose.test.yml run --rm playwright \
  npx playwright test supervisor-progress-indicator.e2e.spec.ts

# Check test results
cat apps/zerg/frontend-web/src/jarvis/test-results/*/error-context.md

# Cleanup
docker compose -f apps/zerg/frontend-web/src/jarvis/docker-compose.test.yml down -v
```

---

## Recommended Fixes (Priority Order)

### P0: Fix Database Initialization

```python
# In start.sh or a new init script, before migrations:
from zerg.database import Base, default_engine
from sqlalchemy import inspect

inspector = inspect(default_engine)
if not inspector.has_table("users"):
    print("Creating initial schema...")
    Base.metadata.create_all(bind=default_engine)
```

### P1: Debug Session Connection

1. Add more logging to frontend session initialization
2. Check `/api/jarvis/session` response in test environment
3. Verify CORS and cookie settings for test domain

### P2: Fix LangGraph Checkpointer

Ensure it reads `DATABASE_URL` from environment, not hardcoded credentials.

### P3: Improve Docker Dev Experience

Consider using Vite dev server inside container for hot reload of TypeScript changes.

---

## Files Involved

| File                                                                         | Issue                          |
| ---------------------------------------------------------------------------- | ------------------------------ |
| `apps/zerg/backend/alembic/versions/458f9a6a8779_initial_schema_baseline.py` | No-op migration                |
| `apps/zerg/backend/start.sh`                                                 | Continues on migration failure |
| `apps/zerg/frontend-web/src/jarvis/docker-compose.test.yml`                                        | Test environment config        |
| `apps/zerg/frontend-web/src/jarvis/lib/supervisor-chat-controller.ts`                     | Session/SSE connection         |
| `apps/zerg/backend/zerg/routers/jarvis.py`                                   | SSE endpoint                   |

---

## Temporary Workaround

To run tests after manually fixing the database:

```bash
# Start services
docker compose -f apps/zerg/frontend-web/src/jarvis/docker-compose.test.yml up -d

# Wait for healthy
sleep 15

# Initialize database manually
docker compose -f apps/zerg/frontend-web/src/jarvis/docker-compose.test.yml exec -T backend python -c "
from zerg.database import initialize_database
initialize_database()
"

# Run tests (still fails due to session connection issue)
docker compose -f apps/zerg/frontend-web/src/jarvis/docker-compose.test.yml run --rm playwright \
  npx playwright test --reporter=list
```

Even with the DB fix, tests still fail because the session connection issue (Bug 2) is unresolved.
