# WebSocket-First Agent Status Updates - Implementation Guide

## Overview

This document outlines the implementation path for converting the agent run button latency fix from **optimistic updates** to a **WebSocket-first architecture**. This approach eliminates the 2-second polling delay by making WebSocket the primary source of truth for agent status updates.

## Current State

### Optimistic Updates (Implemented)
- ✅ Frontend immediately updates UI on button click
- ✅ Uses React Query's `onMutate` for optimistic updates
- ✅ Rollback on error
- ⚠️ Still relies on 2-second polling for eventual consistency
- ⚠️ Multi-user scenarios may show briefly inconsistent state

### Current Pipeline
```
User Click → Optimistic Update (0ms) → HTTP POST (50ms) → Backend (200ms)
                                                      ↓
WebSocket Event ← EventBus ← DB Update (500ms) ← Lock Acquisition
                                                      ↓
UI Refetch ← Polling (2000ms) ← Still polling every 2 seconds!
```

## Target State: WebSocket-First Architecture

### Goals
1. **Remove polling delay** for agent status changes
2. **Instant server confirmation** via WebSocket
3. **Single source of truth** - WebSocket events update UI directly
4. **Reduce server load** by eliminating unnecessary polling
5. **Better multi-user consistency** - all users see updates instantly

### Target Pipeline
```
User Click → HTTP POST (50ms) → Backend starts task (100ms)
                                      ↓
WebSocket Event ← EventBus ← Status Update (150ms)
                                      ↓
UI Updates ← WebSocket Handler (200ms) ← Real-time!
```

## Implementation Plan

### Phase 1: Backend Changes (4-6 hours)

#### 1.1 Modify Task Runner to Return Immediate Status

**File**: `apps/zerg/backend/zerg/services/task_runner.py`

**Changes**:
```python
# Add a new async helper function
async def execute_agent_task_async(
    db: Session,
    agent: AgentModel,
    user_id: int,
    *,
    thread_type: str = "manual"
) -> None:
    """Async wrapper that doesn't block the HTTP response."""
    try:
        # Execute the actual task (moving logic from execute_agent_task)
        await _execute_agent_task_impl(db, agent, user_id, thread_type=thread_type)
    except Exception as exc:
        logger.exception("Task execution failed for agent %s", agent.id)
        # Error handling is already in execute_agent_task

# Refactor execute_agent_task to be async-friendly
async def execute_agent_task(
    db: Session,
    agent: AgentModel,
    *,
    thread_type: str = "manual"
) -> tuple[ThreadModel, bool]:
    """Execute agent task and return (thread, started_immediately).

    Returns:
        thread: The created thread
        started_immediately: True if task started sync (for backward compat)
    """
    # ... validation code ...

    # Acquire lock first
    use_advisory = bool(getattr(db.bind, "dialect", None) and db.bind.dialect.name == "postgresql")
    if not use_advisory:
        raise ValueError("PostgreSQL is required for agent execution (advisory locks)")

    with AgentLockManager.agent_lock(db, agent.id) as acquired:
        if not acquired:
            raise ValueError("Agent already running")

        # Update status to running BEFORE starting
        crud.update_agent(db, agent.id, status="running")
        db.commit()
        await event_bus.publish(
            EventType.AGENT_UPDATED,
            {"event_type": "agent_updated", "id": agent.id, "status": "running"}
        )

        # Now create thread and start execution
        thread = _create_thread_and_run(db, agent, thread_type)

        return thread, True
```

#### 1.2 Update Agent Router to Return Status

**File**: `apps/zerg/backend/zerg/routers/agents.py`

**Changes**:
```python
from typing import TypedDict

class AgentTaskResponse(TypedDict):
    agent_id: int
    status: str
    thread_id: int
    message: str

@router.post("/{agent_id}/task", response_model=AgentTaskResponse)
async def run_agent_task(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    agent = crud.get_agent(db, agent_id)
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    # Authorization check
    is_admin = getattr(current_user, "role", "USER") == "ADMIN"
    if not is_admin and agent.owner_id != current_user.id:
        raise HTTPException(status_code=403, detail="Forbidden: not agent owner")

    from zerg.services.task_runner import execute_agent_task

    try:
        # Execute task and get immediate status
        thread, started = await execute_agent_task(db, agent, thread_type="manual")

        # Return immediate status to client
        return {
            "agent_id": agent_id,
            "status": "running",
            "thread_id": thread.id,
            "message": "Agent started successfully"
        }
    except ValueError as exc:
        if "already running" in str(exc).lower():
            raise HTTPException(status_code=409, detail="Agent already running") from exc
        raise HTTPException(status_code=400, detail=str(exc)) from exc
```

#### 1.3 Ensure WebSocket Event is Published

The event bus already publishes `AGENT_UPDATED` events (see line 106-109 in task_runner.py), which are broadcast via WebSocket. This is already implemented!

**Verification needed**: Ensure the event includes all necessary fields:
```python
await event_bus.publish(
    EventType.AGENT_UPDATED,
    {
        "event_type": "agent_updated",
        "id": agent.id,
        "status": "running",
        "last_error": None,  # Clear any previous errors
        "thread_id": thread.id,  # Include thread ID for reference
    }
)
```

### Phase 2: Frontend Changes (3-4 hours)

#### 2.1 Update API Client to Return Status

**File**: `apps/zerg/frontend-web/src/services/api.ts`

**Changes**:
```typescript
// Update runAgent to return status
export async function runAgent(agentId: number): Promise<{ agent_id: number; status: string; thread_id: number; message: string }> {
  return request<{ agent_id: number; status: string; thread_id: number; message: string }>(`/agents/${agentId}/task`, {
    method: "POST",
  });
}
```

#### 2.2 Simplify Optimistic Update to Use Server Response

**File**: `apps/zerg/frontend-web/src/pages/DashboardPage.tsx`

**Changes**:
```typescript
const runAgentMutation = useMutation({
  mutationFn: runAgent,
  onMutate: async (agentId: number) => {
    // Cancel outgoing refetches
    await queryClient.cancelQueries({ queryKey: ["agents", { scope }] });

    // Snapshot previous value
    const previousAgents = queryClient.getQueryData<AgentSummary[]>(["agents", { scope }]);

    // Optimistic update to "running"
    queryClient.setQueryData<AgentSummary[]>(["agents", { scope }], (old) =>
      old?.map((agent) =>
        agent.id === agentId
          ? { ...agent, status: "running" as const }
          : agent
      )
    );

    return { previousAgents };
  },
  onSuccess: (response, agentId) => {
    // Server confirmed the status - update with actual response
    queryClient.setQueryData<AgentSummary[]>(["agents", { scope }], (old) =>
      old?.map((agent) =>
        agent.id === agentId
          ? { ...agent, status: response.status as any }
          : agent
      )
    );
  },
  onError: (err, agentId, context) => {
    // Rollback on error
    queryClient.setQueryData(["agents", { scope }], context?.previousAgents);
    console.error("Failed to run agent:", err);
  },
  onSettled: (data, error, agentId) => {
    // Sync with server - but this should now be nearly instant
    queryClient.invalidateQueries({ queryKey: ["agents", { scope }] });
    dispatchDashboardEvent("run", agentId);
  },
});
```

#### 2.3 Reduce Polling Interval or Make It Conditional

**File**: `apps/zerg/frontend-web/src/pages/DashboardPage.tsx`

**Option A: Reduce polling to 10 seconds**
```typescript
const { data, isLoading, error, refetch } = useQuery<AgentSummary[]>({
  queryKey: ["agents", { scope }],
  queryFn: () => fetchAgents({ scope }),
  refetchInterval: 10000, // Increased from 2000ms to 10000ms
});
```

**Option B: Smart polling (recommended)**
```typescript
// Track which agents are running to poll them more frequently
const [runningAgentIds, setRunningAgentIds] = useState<Set<number>>(new Set());

// Use different intervals for different scopes
const getRefetchInterval = () => {
  if (scope === "my" && runningAgentIds.size > 0) {
    return 2000; // Poll more frequently when agents are running
  }
  return 10000; // Poll less frequently when no agents are running
};

const { data, isLoading, error, refetch } = useQuery<AgentSummary[]>({
  queryKey: ["agents", { scope }],
  queryFn: () => fetchAgents({ scope }),
  refetchInterval: getRefetchInterval(),
});
```

### Phase 3: WebSocket Enhancement (2-3 hours)

#### 3.1 Ensure Agent Events Are Broadcast

**File**: `apps/zerg/backend/zerg/websocket/manager.py`

**Verification** - The `_handle_agent_event` method (lines 477-494) already handles this:

```python
async def _handle_agent_event(self, data: Dict[str, Any]) -> None:
    """Handle agent-related events from the event bus."""
    if "id" not in data:
        return

    agent_id = data["id"]
    topic = f"agent:{agent_id}"

    # Create envelope
    envelope = Envelope.create(
        message_type=event_type,
        topic=topic,
        data=serialized_data
    )
    await self.broadcast_to_topic(topic, envelope.model_dump())
```

**Ensure clients subscribe to agent topics**:

In `apps/zerg/frontend-web/src/pages/DashboardPage.tsx`, update the WebSocket subscription:

```typescript
// Subscribe to specific agent topics for real-time updates
useWebSocket(isAuthenticated, {
  includeAuth: true,
  invalidateQueries: [["agents", { scope }]],
  onMessage: (message) => {
    // If this is an agent update, invalidate immediately
    if (message.type === "agent_updated") {
      queryClient.invalidateQueries({ queryKey: ["agents", { scope }] });
    } else {
      // Default behavior
      void refetch();
    }
  },
});
```

#### 3.2 Add WebSocket Message Handler for Agent Updates

**File**: `apps/zerg/frontend-web/src/lib/useWebSocket.tsx`

**Enhancement**:
```typescript
const handleMessage = useCallback((event: MessageEvent) => {
  let message: WebSocketMessage;

  try {
    message = JSON.parse(event.data);
  } catch {
    message = { type: 'message', data: event.data };
  }

  // Handle agent-specific updates specially
  if (message.type === 'agent_updated' || message.type === 'run_update') {
    // Immediately invalidate agent queries
    invalidateQueriesRef.current.forEach(queryKey => {
      queryClient.invalidateQueries({ queryKey });
    });
    return; // Don't process further
  }

  // ... rest of existing code ...
}, [queryClient]);
```

### Phase 4: Testing (2-3 hours)

#### 4.1 Update E2E Test

**File**: `apps/zerg/e2e/tests/agent_run_optimistic_update.spec.ts`

**Update test to verify WebSocket behavior**:
```typescript
test('should update status via WebSocket, not polling', async ({ page }) => {
  // Navigate to dashboard
  await page.goto('/dashboard');

  // Create agent
  await page.locator('[data-testid="create-agent-btn"]').click();

  const agentRow = page.locator('tr[data-agent-id]').first();
  const agentId = await agentRow.getAttribute('data-agent-id');
  const statusCell = agentRow.locator('td[data-label="Status"]');

  // Track network requests to verify POST is made
  const apiCalls: string[] = [];
  page.on('response', async (response) => {
    if (response.url().includes(`/api/agents/${agentId}/task`)) {
      apiCalls.push(`POST ${response.url()} - ${response.status()}`);
    }
  });

  // Track WebSocket messages
  const wsMessages: string[] = [];
  page.on('console', (msg) => {
    if (msg.text().includes('agent_updated')) {
      wsMessages.push(msg.text());
    }
  });

  // Click run button
  const runButton = page.locator(`[data-testid="run-agent-${agentId}"]`);
  await runButton.click();

  // Verify optimistic update happens
  await expect(async () => {
    const status = await statusCell.textContent();
    expect(status).toContain('Running');
  }).toPass({ timeout: 500 });

  // Wait for WebSocket update (should be faster than 2 seconds)
  await page.waitForTimeout(1500);

  // Verify WebSocket message was received
  expect(wsMessages.length).toBeGreaterThan(0);

  // Verify status is still "Running"
  const finalStatus = await statusCell.textContent();
  expect(finalStatus).toContain('Running');

  console.log('✅ WebSocket-first update test passed');
});
```

#### 4.2 Add Backend Test

**File**: `apps/zerg/backend/tests/test_websocket_agent_updates.py`

```python
import pytest
from fastapi import WebSocket
from zerg.events import EventType
from zerg.events.event_bus import event_bus

@pytest.mark.asyncio
async def test_agent_run_emits_websocket_event():
    """Verify that running an agent emits a WebSocket event."""
    # Setup: Create agent and mock user
    # ... test setup ...

    # Execute: Call run_agent_task
    # ... execute task ...

    # Assert: Check that event was published
    # ... assertions ...

@pytest.mark.asyncio
async def test_multiple_subscribers_receive_agent_update():
    """Verify that multiple WebSocket clients receive agent updates."""
    # Setup: Create multiple WebSocket connections
    # ... setup connections ...

    # Execute: Update agent status
    # ... trigger update ...

    # Assert: All clients receive the update
    # ... assertions ...
```

### Phase 5: Performance Validation (1-2 hours)

#### 5.1 Measure Latency

Add performance monitoring:

```typescript
// In DashboardPage.tsx
const handleRunAgent = useCallback((event, agentId, status) => {
  const startTime = performance.now();

  runAgentMutation.mutate(agentId, {
    onSuccess: () => {
      const endTime = performance.now();
      const latency = endTime - startTime;
      console.log(`Agent run latency: ${latency.toFixed(2)}ms`);

      // Alert if latency is too high
      if (latency > 500) {
        console.warn(`High latency detected: ${latency.toFixed(2)}ms`);
      }
    }
  });
}, [runAgentMutation]);
```

#### 5.2 Monitor Polling Reduction

Add metrics to track polling frequency:

```typescript
// Track how often we're refetching
useEffect(() => {
  const intervalId = setInterval(() => {
    const cacheData = queryClient.getQueryState(["agents", { scope }])?.data;
    if (cacheData) {
      console.log(`Polling refetch at ${new Date().toISOString()}`);
    }
  }, 2000);

  return () => clearInterval(intervalId);
}, [scope]);
```

## Migration Strategy

### Step 1: Deploy Backend Changes First
1. Deploy the updated `task_runner.py` with immediate status publishing
2. Deploy the updated `agents.py` router with status response
3. Verify events are being published correctly

### Step 2: Deploy Frontend Optimistically
1. Deploy the updated API client
2. Keep optimistic updates for now
3. Monitor WebSocket messages

### Step 3: Verify WebSocket is Working
1. Check browser console for WebSocket messages
2. Verify agent status updates in real-time
3. Test with multiple browser tabs

### Step 4: Reduce Polling
1. Increase polling interval from 2s to 10s
2. Monitor for any issues
3. Eventually remove polling for agents that are stable

### Step 5: Remove Optimistic Updates (Optional)
1. If WebSocket is reliable, can remove optimistic updates
2. Or keep them for "instant" feel with WebSocket for confirmation

## Testing Checklist

- [ ] Backend: Agent run publishes WebSocket event
- [ ] Backend: Event includes all necessary fields (id, status, last_error, thread_id)
- [ ] Frontend: Receives WebSocket messages for agent updates
- [ ] Frontend: UI updates without polling delay
- [ ] E2E: Test passes with <500ms latency
- [ ] E2E: Test with multiple agents
- [ ] E2E: Test handles WebSocket errors gracefully
- [ ] Performance: No regression in page load time
- [ ] Multi-user: Updates reflect across browser tabs

## Rollback Plan

If issues arise:

1. **Backend**: Revert to previous `agents.py` and `task_runner.py` - optimistic updates will still work
2. **Frontend**: Keep optimistic updates as fallback - WebSocket becomes enhancement
3. **Polling**: Increase interval back to 2s if needed

## Success Metrics

| Metric | Current | Target | Measurement |
|--------|---------|--------|-------------|
| Time to status change | 2000ms | <500ms | E2E test |
| WebSocket message received | N/A | <200ms | Browser console |
| Polling requests/min | 30 | 5-10 | Network tab |
| User perception | "Feels broken" | "Instant" | Manual testing |

## References

- **EventBus Documentation**: `apps/zerg/backend/zerg/events/event_bus.py`
- **WebSocket Manager**: `apps/zerg/backend/zerg/websocket/manager.py`
- **WebSocket Router**: `apps/zerg/backend/zerg/routers/websocket.py`
- **Current Optimistic Implementation**: `apps/zerg/frontend-web/src/pages/DashboardPage.tsx` (lines 66-99)
- **React Query Optimistic Updates**: https://tanstack.com/query/latest/docs/framework/react/guides/optimistic-updates

## Estimated Total Time

- **Backend Changes**: 4-6 hours
- **Frontend Changes**: 3-4 hours
- **WebSocket Enhancement**: 2-3 hours
- **Testing**: 2-3 hours
- **Performance Validation**: 1-2 hours
- **Total**: 12-18 hours

## Priority

**Medium-High**. The optimistic updates provide most of the user experience benefit with much less complexity. The WebSocket-first approach is valuable for:
- High-traffic deployments (reduces polling load)
- Multi-user scenarios (better consistency)
- Production-grade real-time features

**Recommendation**: Deploy optimistic updates first (✅ Done), then evaluate if WebSocket-first is needed based on user feedback and traffic patterns.
