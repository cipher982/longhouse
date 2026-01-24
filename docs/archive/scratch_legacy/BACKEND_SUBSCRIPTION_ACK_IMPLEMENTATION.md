# Backend Subscription Acknowledgment Implementation Guide

**Issue:** Frontend expects `subscribe_ack` and `subscribe_error` messages that backend doesn't send

**Priority:** Critical - Blocks production deployment of subscription confirmation feature

**Estimated Effort:** 2-4 hours development, 1-2 hours testing

---

## Overview

The frontend in commit 06a6365 implements subscription confirmation tracking, but the backend doesn't send the required acknowledgment messages. This causes all subscriptions to timeout after 5 seconds, leading to subscription churn and increased WebSocket traffic.

**Required Changes:**
1. Send `subscribe_ack` after successful subscriptions
2. Send `subscribe_error` on subscription failures
3. Add Pydantic schemas for message types
4. Update tests

---

## Implementation

### Step 1: Add Pydantic Schemas

**File:** `/apps/zerg/backend/zerg/schemas/ws_messages.py` or `/apps/zerg/backend/zerg/generated/ws_messages.py`

**Add these classes:**

```python
from pydantic import BaseModel
from typing import List

class SubscribeAckData(BaseModel):
    """Acknowledgment sent after successful subscription."""
    message_id: str
    topics: List[str]

class SubscribeErrorData(BaseModel):
    """Error response for failed subscription."""
    message_id: str
    topics: List[str]
    error: str
```

**Update the message type enum:**

```python
class MessageType(str, Enum):
    # ... existing types ...
    SUBSCRIBE_ACK = "subscribe_ack"
    SUBSCRIBE_ERROR = "subscribe_error"
```

### Step 2: Update Subscription Handlers

**File:** `/apps/zerg/backend/zerg/websocket/handlers.py`

#### Change 1: Update `_subscribe_agent`

**Current code (lines 215-263):**
```python
async def _subscribe_agent(client_id: str, agent_id: int, message_id: str, db: Session) -> None:
    try:
        agent = crud.get_agent(db, agent_id)
        if not agent:
            await send_error(client_id, f"Agent {agent_id} not found", message_id)
            return

        topic = f"agent:{agent_id}"
        await topic_manager.subscribe_to_topic(client_id, topic)

        # Send initial agent state
        agent_data = AgentEventData(...)
        envelope = Envelope.create(
            message_type="agent_state",
            topic=topic,
            data=agent_data.model_dump(),
            req_id=message_id,
        )
        await send_to_client(client_id, envelope.model_dump())
        logger.info(f"Sent initial agent_state for agent {agent_id} to client {client_id}")

    except Exception as e:
        logger.error(f"Error in _subscribe_agent: {str(e)}")
        await send_error(client_id, "Failed to subscribe to agent", message_id)
```

**Updated code with ack:**
```python
async def _subscribe_agent(client_id: str, agent_id: int, message_id: str, db: Session) -> None:
    try:
        agent = crud.get_agent(db, agent_id)
        if not agent:
            # Send subscribe_error instead of generic error
            error_data = SubscribeErrorData(
                message_id=message_id,
                topics=[f"agent:{agent_id}"],
                error=f"Agent {agent_id} not found"
            )
            error_envelope = Envelope.create(
                message_type=MessageType.SUBSCRIBE_ERROR,
                topic="system",
                data=error_data.model_dump(),
                req_id=message_id,
            )
            await send_to_client(client_id, error_envelope.model_dump())
            return

        topic = f"agent:{agent_id}"
        await topic_manager.subscribe_to_topic(client_id, topic)

        # Send initial agent state
        agent_data = AgentEventData(...)
        envelope = Envelope.create(
            message_type="agent_state",
            topic=topic,
            data=agent_data.model_dump(),
            req_id=message_id,
        )
        await send_to_client(client_id, envelope.model_dump())

        # ✅ NEW: Send subscription acknowledgment
        ack_data = SubscribeAckData(
            message_id=message_id,
            topics=[topic]
        )
        ack_envelope = Envelope.create(
            message_type=MessageType.SUBSCRIBE_ACK,
            topic="system",
            data=ack_data.model_dump(),
            req_id=message_id,
        )
        await send_to_client(client_id, ack_envelope.model_dump())

        logger.info(f"Sent subscribe_ack for agent {agent_id} to client {client_id}")

    except Exception as e:
        logger.error(f"Error in _subscribe_agent: {str(e)}")
        # Send subscribe_error instead of generic error
        error_data = SubscribeErrorData(
            message_id=message_id,
            topics=[f"agent:{agent_id}"],
            error=f"Failed to subscribe to agent: {str(e)}"
        )
        error_envelope = Envelope.create(
            message_type=MessageType.SUBSCRIBE_ERROR,
            topic="system",
            data=error_data.model_dump(),
            req_id=message_id,
        )
        await send_to_client(client_id, error_envelope.model_dump())
```

#### Change 2: Update `_subscribe_user`

**Add after line 319 (after sending user_update):**
```python
# Send subscription acknowledgment
ack_data = SubscribeAckData(
    message_id=message_id,
    topics=[topic]
)
ack_envelope = Envelope.create(
    message_type=MessageType.SUBSCRIBE_ACK,
    topic="system",
    data=ack_data.model_dump(),
    req_id=message_id,
)
await send_to_client(client_id, ack_envelope.model_dump())

logger.info("Sent subscribe_ack for user %s to client %s", user_id, client_id)
```

**Update error handling (around line 296):**
```python
if user is None:
    error_data = SubscribeErrorData(
        message_id=message_id,
        topics=[f"user:{user_id}"],
        error=f"User {user_id} not found"
    )
    error_envelope = Envelope.create(
        message_type=MessageType.SUBSCRIBE_ERROR,
        topic="system",
        data=error_data.model_dump(),
        req_id=message_id,
    )
    await send_to_client(client_id, error_envelope.model_dump())
    return
```

#### Change 3: Update `_subscribe_workflow_execution`

**Add after line 397 (after sending snapshot or subscribing):**
```python
# Send subscription acknowledgment
ack_data = SubscribeAckData(
    message_id=message_id,
    topics=[topic]
)
ack_envelope = Envelope.create(
    message_type=MessageType.SUBSCRIBE_ACK,
    topic="system",
    data=ack_data.model_dump(),
    req_id=message_id,
)
await send_to_client(client_id, ack_envelope.model_dump())

logger.info("Sent subscribe_ack for workflow_execution %s to client %s", execution_id, client_id)
```

**Update error handling (around line 399-401):**
```python
except Exception as e:
    logger.error(f"Error in _subscribe_workflow_execution: {str(e)}")
    error_data = SubscribeErrorData(
        message_id=message_id,
        topics=[f"workflow_execution:{execution_id}"],
        error=f"Failed to subscribe to workflow execution: {str(e)}"
    )
    error_envelope = Envelope.create(
        message_type=MessageType.SUBSCRIBE_ERROR,
        topic="system",
        data=error_data.model_dump(),
        req_id=message_id,
    )
    await send_to_client(client_id, error_envelope.model_dump())
```

#### Change 4: Update `_subscribe_ops_events`

**Add after line 418 (after subscription):**
```python
# Send subscription acknowledgment
ack_data = SubscribeAckData(
    message_id=message_id,
    topics=[topic]
)
ack_envelope = Envelope.create(
    message_type=MessageType.SUBSCRIBE_ACK,
    topic="system",
    data=ack_data.model_dump(),
    req_id=message_id,
)
await send_to_client(client_id, ack_envelope.model_dump())

logger.info("Client %s subscribed to ops:events with ack sent", client_id)
```

**Update error handling (around line 410-414):**
```python
user_id = topic_manager.client_users.get(client_id)
if not user_id:
    error_data = SubscribeErrorData(
        message_id=message_id,
        topics=["ops:events"],
        error="Unauthorized"
    )
    error_envelope = Envelope.create(
        message_type=MessageType.SUBSCRIBE_ERROR,
        topic="system",
        data=error_data.model_dump(),
        req_id=message_id,
    )
    await send_to_client(client_id, error_envelope.model_dump(), topic="system")
    return

user = crud.get_user(db, int(user_id))
if user is None or getattr(user, "role", "USER") != "ADMIN":
    error_data = SubscribeErrorData(
        message_id=message_id,
        topics=["ops:events"],
        error="Admin privileges required for ops:events"
    )
    error_envelope = Envelope.create(
        message_type=MessageType.SUBSCRIBE_ERROR,
        topic="system",
        data=error_data.model_dump(),
        req_id=message_id,
    )
    await send_to_client(client_id, error_envelope.model_dump())
    # Note: Still close with code 4403 if needed
    return
```

#### Change 5: Update `handle_subscribe` error cases

**Around line 443-447 (thread subscriptions):**
```python
if topic_type == "thread":
    error_data = SubscribeErrorData(
        message_id=subscribe_data.message_id,
        topics=[topic],
        error="Thread subscriptions no longer supported. All streaming is delivered to user:{user_id}"
    )
    error_envelope = Envelope.create(
        message_type=MessageType.SUBSCRIBE_ERROR,
        topic="system",
        data=error_data.model_dump(),
        req_id=subscribe_data.message_id,
    )
    await send_to_client(client_id, error_envelope.model_dump())
```

**Around line 457-461 (invalid topic types):**
```python
else:
    error_data = SubscribeErrorData(
        message_id=subscribe_data.message_id,
        topics=[topic],
        error=f"Invalid topic format: Unsupported topic type '{topic_type}'"
    )
    error_envelope = Envelope.create(
        message_type=MessageType.SUBSCRIBE_ERROR,
        topic="system",
        data=error_data.model_dump(),
        req_id=subscribe_data.message_id,
    )
    await send_to_client(client_id, error_envelope.model_dump())
```

**Around line 462-467 (ValueError parsing):**
```python
except ValueError as e:
    error_data = SubscribeErrorData(
        message_id=subscribe_data.message_id,
        topics=[topic],
        error=f"Invalid topic format: {topic}. Error: {str(e)}"
    )
    error_envelope = Envelope.create(
        message_type=MessageType.SUBSCRIBE_ERROR,
        topic="system",
        data=error_data.model_dump(),
        req_id=subscribe_data.message_id,
    )
    await send_to_client(client_id, error_envelope.model_dump())
```

### Step 3: Add Imports

**At the top of handlers.py, add:**
```python
from zerg.schemas.ws_messages import SubscribeAckData, SubscribeErrorData
# OR if using generated:
from zerg.generated.ws_messages import SubscribeAckData, SubscribeErrorData
```

### Step 4: Update Inbound Schema Map

**Around line 522-529, add:**
```python
_INBOUND_SCHEMA_MAP: Dict[str, type[BaseModel]] = {
    "ping": PingData,
    "pong": PongData,
    "subscribe": SubscribeData,
    "unsubscribe": UnsubscribeData,
    "send_message": SendMessageData,
    # Note: subscribe_ack and subscribe_error are outbound only, not in this map
}
```

---

## Testing

### Step 1: Add Unit Tests

**Create/update:** `/apps/zerg/backend/tests/test_websocket_subscription_ack.py`

```python
import pytest
from zerg.websocket.handlers import _subscribe_agent, _subscribe_user
from zerg.generated.ws_messages import Envelope, MessageType

@pytest.mark.asyncio
async def test_subscribe_agent_sends_ack(test_db, test_client, test_agent):
    """Test that subscribing to an agent sends subscribe_ack."""
    client_id = "test-client-123"
    message_id = "test-msg-456"

    # Mock the topic_manager and send_to_client
    messages_sent = []

    async def mock_send(client_id, message, **kwargs):
        messages_sent.append(message)

    # Subscribe to agent
    with patch("zerg.websocket.handlers.send_to_client", mock_send):
        await _subscribe_agent(client_id, test_agent.id, message_id, test_db)

    # Should have sent: agent_state + subscribe_ack
    assert len(messages_sent) == 2

    # Verify subscribe_ack message
    ack_msg = messages_sent[1]
    assert ack_msg["type"] == "subscribe_ack"
    assert ack_msg["data"]["message_id"] == message_id
    assert f"agent:{test_agent.id}" in ack_msg["data"]["topics"]

@pytest.mark.asyncio
async def test_subscribe_nonexistent_agent_sends_error(test_db, test_client):
    """Test that subscribing to non-existent agent sends subscribe_error."""
    client_id = "test-client-123"
    message_id = "test-msg-456"

    messages_sent = []

    async def mock_send(client_id, message, **kwargs):
        messages_sent.append(message)

    # Subscribe to non-existent agent
    with patch("zerg.websocket.handlers.send_to_client", mock_send):
        await _subscribe_agent(client_id, 99999, message_id, test_db)

    # Should have sent: subscribe_error only
    assert len(messages_sent) == 1

    # Verify subscribe_error message
    error_msg = messages_sent[0]
    assert error_msg["type"] == "subscribe_error"
    assert error_msg["data"]["message_id"] == message_id
    assert "not found" in error_msg["data"]["error"]
    assert "agent:99999" in error_msg["data"]["topics"]
```

### Step 2: Add Integration Tests

**Create/update:** `/apps/zerg/backend/tests/test_websocket_integration.py`

```python
@pytest.mark.asyncio
async def test_subscribe_receives_ack(test_client_ws):
    """End-to-end test: subscribe and receive ack."""
    # Send subscribe message
    await test_client_ws.send_json({
        "type": "subscribe",
        "topics": ["agent:1"],
        "message_id": "test-subscribe-1"
    })

    # Receive messages
    messages = []
    for _ in range(3):  # agent_state, subscribe_ack, maybe others
        msg = await test_client_ws.receive_json(timeout=2)
        messages.append(msg)

    # Find subscribe_ack
    ack = next((m for m in messages if m["type"] == "subscribe_ack"), None)
    assert ack is not None, "Did not receive subscribe_ack"
    assert ack["data"]["message_id"] == "test-subscribe-1"
    assert "agent:1" in ack["data"]["topics"]
```

### Step 3: Run Existing E2E Tests

```bash
cd apps/zerg/e2e
npx playwright test websocket_subscription_confirmation.spec.ts
```

**Expected Results After Backend Changes:**
- Test "should handle subscription ack when backend implements it" should now pass
- Test "should handle subscription timeout" should show 0 timeouts
- All 10 tests should pass

---

## Validation Checklist

After implementing changes:

- [ ] Pydantic schemas added for SubscribeAckData and SubscribeErrorData
- [ ] MessageType enum includes SUBSCRIBE_ACK and SUBSCRIBE_ERROR
- [ ] All subscription handlers send subscribe_ack on success
- [ ] All error paths send subscribe_error instead of generic error
- [ ] Unit tests pass (test_websocket_subscription_ack.py)
- [ ] Integration tests pass (test_websocket_integration.py)
- [ ] Backend tests run successfully: `pytest tests/test_websocket*.py`
- [ ] E2E tests now receive ack messages: `npx playwright test websocket_subscription_confirmation.spec.ts`
- [ ] No subscription timeouts in frontend console
- [ ] WebSocket traffic reduced (verify in browser DevTools)
- [ ] No console warnings about subscription timeouts
- [ ] Existing functionality still works (agents list, real-time updates)

---

## Expected Behavior After Implementation

### Before (Current)
```
Frontend: Send subscribe message (message_id: abc123)
Backend:  → (no ack sent)
Frontend: Wait 5 seconds...
Frontend: Timeout warning in console
Frontend: Remove from subscribed set
Frontend: (later) Re-subscribe with new message_id
```

### After (Fixed)
```
Frontend: Send subscribe message (message_id: abc123)
Backend:  → Send initial agent_state
Backend:  → Send subscribe_ack (message_id: abc123)
Frontend: Receive ack, clear timeout
Frontend: ✅ Subscription confirmed
(No timeout, no re-subscribe)
```

---

## Performance Impact

### Expected Improvements
- **Eliminate timeouts:** 0 subscription timeouts vs current 100%
- **Reduce WebSocket traffic:** 66% reduction (no re-subscriptions)
- **Reduce CPU:** No timeout management overhead
- **Cleaner logs:** No timeout warnings

### Measurement
Monitor these metrics before/after:
```python
# Add to backend
subscription_acks_sent = Counter("ws_subscription_acks_total")
subscription_errors_sent = Counter("ws_subscription_errors_total")
subscription_latency = Histogram("ws_subscription_ack_latency_seconds")
```

---

## Rollback Plan

If issues arise after deployment:

1. **Quick rollback:** Revert backend changes (keeps frontend code, falls back to timeout behavior)
2. **Monitor:** Check for subscription errors in logs
3. **Debug:** Use E2E tests to reproduce issues
4. **Fix forward:** Address specific issues identified

The frontend is designed to handle missing acks gracefully (via timeout), so rollback is low-risk.

---

## Questions?

**See full documentation:**
- Main review: `/apps/zerg/e2e/WEBSOCKET_FIXES_REVIEW.md`
- Summary: `/WEBSOCKET_REVIEW_SUMMARY.md`
- Test files: `/apps/zerg/e2e/tests/websocket_*.spec.ts`

**Contact:** Backend team lead

**Priority:** Critical (blocks Issue #7 production deployment)

**Timeline:** 1-2 days for full implementation and validation
