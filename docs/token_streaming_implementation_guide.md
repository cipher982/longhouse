# Token-Level Streaming Implementation Guide

## Executive Summary

Token-level streaming is **partially implemented** in the codebase. The backend infrastructure exists and can emit tokens, but:

1. **Backend Issue**: Callbacks aren't being passed to the LLM during invocation
2. **Frontend Issue**: No handling of streaming messages in the chat interface
3. **Connection Issue**: Frontend doesn't subscribe to thread topics to receive streaming messages

## Current State Analysis

### Backend Infrastructure ✅ (Mostly Complete)

#### 1. Token Callback System
**Location**: `apps/zerg/backend/zerg/callbacks/token_stream.py`

The `WsTokenCallback` class is properly implemented:
- Extends `AsyncCallbackHandler` from LangChain
- Implements `on_llm_new_token()` to broadcast tokens via WebSocket
- Uses context variables (`current_thread_id_var`) to track which thread is streaming
- Filters out unwanted callback types (chains, agents, etc.)

**How it works**:
```python
async def on_llm_new_token(self, token: str, **_: Any) -> None:
    thread_id = current_thread_id_var.get()
    topic = f"thread:{thread_id}"
    
    chunk_data = StreamChunkData(
        thread_id=thread_id,
        content=token,
        chunk_type="assistant_token",
        ...
    )
    await topic_manager.broadcast_to_topic(topic, envelope.model_dump())
```

#### 2. AgentRunner Context Management ✅
**Location**: `apps/zerg/backend/zerg/managers/agent_runner.py`

The `AgentRunner` sets the thread context before invoking the LLM:
```python
_ctx_token = set_current_thread_id(thread.id)
try:
    updated_messages = await self._runnable.ainvoke(original_msgs, config)
finally:
    set_current_thread_id(None)  # Clean up
```

#### 3. Feature Flag ✅
**Location**: `apps/zerg/backend/zerg/config/__init__.py`

Token streaming is controlled by `LLM_TOKEN_STREAM` environment variable (via `get_settings().llm_token_stream`).

#### 4. LLM Configuration ✅ (Partial)
**Location**: `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py`

The LLM is created with `streaming=True` when enabled:
```python
kwargs = {
    "model": agent_row.model,
    "streaming": enable_token_stream,  # ✅ Set correctly
    ...
}
llm = ChatOpenAI(**kwargs)
```

### Critical Backend Issue ❌

**Problem**: Callbacks are **NOT being passed** to the LLM during invocation!

**Location**: `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py` lines 149, 156

Current code:
```python
def _call_model_sync(messages: List[BaseMessage]):
    return llm_with_tools.invoke(messages)  # ❌ No callbacks!

async def _call_model_async(messages: List[BaseMessage]):
    return await asyncio.to_thread(_call_model_sync, messages)  # ❌ No callbacks!
```

**Fix Required**: Pass `WsTokenCallback` instance via `config` parameter during `ainvoke()`:

```python
from zerg.callbacks.token_stream import WsTokenCallback

async def _call_model_async(messages: List[BaseMessage], enable_token_stream: bool):
    if enable_token_stream:
        callback = WsTokenCallback()
        # Pass callbacks via config - LangChain will call on_llm_new_token during streaming
        # Note: ainvoke() with streaming=True still triggers callbacks, even though it returns full message
        result = await llm_with_tools.ainvoke(
            messages,
            config={"callbacks": [callback]}
        )
        return result
    else:
        # For non-streaming, use sync invoke wrapped in thread
        return await asyncio.to_thread(llm_with_tools.invoke, messages)
```

**Note**: LangChain's `ainvoke()` with `streaming=True` **will trigger callbacks** during execution. The callbacks receive tokens as they arrive via `on_llm_new_token()`, while `ainvoke()` still returns the complete final message. This is the correct approach for our use case since we want:
- Token callbacks to stream via WebSocket (handled by `WsTokenCallback.on_llm_new_token()`)
- Full message returned to the agent executor (handled by `ainvoke()` return value)

### Frontend Infrastructure ❌ (Missing)

#### 1. WebSocket Connection ✅
**Location**: `apps/zerg/frontend-web/src/lib/useWebSocket.tsx`

The WebSocket hook exists and connects, but:
- Only invalidates queries on message receipt
- **Does NOT subscribe to thread topics**
- **Does NOT handle streaming message types**

#### 2. Chat Page ❌
**Location**: `apps/zerg/frontend-web/src/pages/ChatPage.tsx`

Current issues:
- Uses `useWebSocket` but only for query invalidation
- **No subscription to `thread:{threadId}` topic**
- **No handling of `stream_chunk`, `stream_start`, `stream_end`, or `assistant_id` messages**
- Messages are rendered from React Query cache only

#### 3. Streaming State Management ❌
- No local state for accumulating streaming tokens
- No optimistic message creation for streaming responses
- No handling of partial content updates

### WebSocket Message Flow

#### Backend Emits:
1. **`stream_start`**: When agent turn begins
2. **`assistant_id`**: (If token streaming) Links streaming to message ID
3. **`stream_chunk`**: Multiple messages with `chunk_type`:
   - `"assistant_token"`: Individual LLM tokens (token streaming mode)
   - `"assistant_message"`: Full message (non-token mode)
   - `"tool_output"`: Tool execution results
4. **`stream_end`**: When agent turn completes

#### Frontend Must:
1. Subscribe to `thread:{threadId}` topic on WebSocket
2. Handle `stream_start` → Create optimistic assistant message bubble
3. Handle `assistant_id` → Store message ID for accumulating tokens
4. Handle `stream_chunk` with `chunk_type="assistant_token"` → Append to message content
5. Handle `stream_end` → Finalize message, refresh from API

## Implementation Roadmap

### Phase 1: Fix Backend Callback Integration

#### Step 1.1: Pass Callbacks During LLM Invocation
**File**: `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py`

**Solution: Use `ainvoke()` with callbacks** (Simplest and correct approach)

Based on LangChain 0.3.x API, `ainvoke()` with `streaming=True` and callbacks passed will:
- Trigger `on_llm_new_token()` callbacks as tokens arrive
- Still return the complete final AIMessage

```python
async def _call_model_async(messages: List[BaseMessage], enable_token_stream: bool):
    """Run LLM with optional token streaming."""
    if enable_token_stream:
        from zerg.callbacks.token_stream import WsTokenCallback
        
        callback = WsTokenCallback()
        # Pass callbacks - LangChain will call on_llm_new_token during streaming
        # ainvoke() returns the complete message while callbacks stream tokens
        result = await llm_with_tools.ainvoke(
            messages,
            config={"callbacks": [callback]}
        )
        return result
    else:
        # For non-streaming, use sync invoke wrapped in thread
        return await asyncio.to_thread(llm_with_tools.invoke, messages)
```

**Why this works**: 
- LangChain's `ChatOpenAI` with `streaming=True` internally streams tokens
- When callbacks are provided via `config`, each token triggers `on_llm_new_token()`
- The final accumulated message is still returned by `ainvoke()`
- This gives us both: token-level streaming (via callbacks) and full message (via return value)

#### Step 1.2: Update Agent Executor to Pass Streaming Flag
**File**: `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py`

Modify `_agent_executor_async` to pass `enable_token_stream`:
```python
async def _agent_executor_async(
    messages: List[BaseMessage], 
    *, 
    previous: Optional[List[BaseMessage]] = None,
    enable_token_stream: bool = False
) -> List[BaseMessage]:
    # ...
    llm_response = await _call_model_async(current_messages, enable_token_stream)
    # ...
```

And update the `get_runnable` function to capture the flag:
```python
def get_runnable(agent_row):
    enable_token_stream = get_settings().llm_token_stream
    
    async def _agent_executor_async_with_flag(messages, *, previous=None):
        return await _agent_executor_async(
            messages, 
            previous=previous,
            enable_token_stream=enable_token_stream
        )
    # ...
```

### Phase 2: Frontend Streaming Support

#### Step 2.1: Extend WebSocket Hook for Message Handling
**File**: `apps/zerg/frontend-web/src/lib/useWebSocket.tsx`

Add streaming message handler:
```typescript
interface UseWebSocketOptions {
  // ... existing options
  onStreamingMessage?: (envelope: WebSocketEnvelope) => void;
}

// In handleMessage:
if (message.type === 'stream_start' || 
    message.type === 'stream_chunk' || 
    message.type === 'stream_end' ||
    message.type === 'assistant_id') {
  options.onStreamingMessage?.(message);
}
```

#### Step 2.2: Subscribe to Thread Topics
**File**: `apps/zerg/frontend-web/src/pages/ChatPage.tsx`

Add subscription when thread changes:
```typescript
const { sendMessage: wsSendMessage } = useWebSocket(agentId != null, {
  includeAuth: true,
  invalidateQueries: wsQueries,
  onStreamingMessage: handleStreamingMessage, // New handler
});

useEffect(() => {
  if (effectiveThreadId && wsSendMessage) {
    // Subscribe to thread topic
    wsSendMessage({
      type: 'subscribe_thread',
      thread_id: effectiveThreadId,
      message_id: `sub-${Date.now()}`,
    });
  }
}, [effectiveThreadId, wsSendMessage]);
```

#### Step 2.3: Streaming State Management
**File**: `apps/zerg/frontend-web/src/pages/ChatPage.tsx`

Add streaming state:
```typescript
const [streamingMessages, setStreamingMessages] = useState<Map<number, string>>(new Map());
const [streamingMessageId, setStreamingMessageId] = useState<number | null>(null);

function handleStreamingMessage(envelope: any) {
  const { type, data } = envelope;
  
  if (type === 'stream_start') {
    // Create optimistic message
    setStreamingMessageId(null); // Will be set by assistant_id
    setStreamingMessages(new Map());
  }
  
  if (type === 'assistant_id') {
    setStreamingMessageId(data.message_id);
    setStreamingMessages(prev => {
      const next = new Map(prev);
      next.set(data.message_id, '');
      return next;
    });
  }
  
  if (type === 'stream_chunk') {
    if (data.chunk_type === 'assistant_token' && streamingMessageId) {
      setStreamingMessages(prev => {
        const next = new Map(prev);
        const current = next.get(streamingMessageId) || '';
        next.set(streamingMessageId, current + data.content);
        return next;
      });
    }
  }
  
  if (type === 'stream_end') {
    // Finalize: refresh messages from API
    queryClient.invalidateQueries({ 
      queryKey: ["thread-messages", data.thread_id] 
    });
    setStreamingMessageId(null);
    setStreamingMessages(new Map());
  }
}
```

#### Step 2.4: Render Streaming Messages
**File**: `apps/zerg/frontend-web/src/pages/ChatPage.tsx`

Update message rendering to show streaming content:
```typescript
{messages.map((msg) => {
  const streamingContent = streamingMessages.get(msg.id);
  const displayContent = streamingContent !== undefined 
    ? streamingContent 
    : msg.content;
  
  return (
    <div key={msg.id}>
      <div className="message-content">
        {displayContent}
        {streamingMessageId === msg.id && (
          <span className="streaming-cursor">▋</span>
        )}
      </div>
    </div>
  );
})}
```

### Phase 3: Testing & Validation

#### 3.1: Backend Tests
- Verify callbacks are called during LLM invocation
- Verify WebSocket messages are emitted
- Test with `LLM_TOKEN_STREAM=true` and `false`

#### 3.2: Frontend E2E Tests
- Test subscription to thread topic
- Test receiving and rendering streaming tokens
- Test message finalization after stream_end

#### 3.3: Integration Tests
- Full flow: Send message → Receive tokens → Display in UI
- Verify no duplicate messages (optimistic + final)
- Verify tool outputs during streaming

## Key Files to Modify

### Backend
1. `apps/zerg/backend/zerg/agents_def/zerg_react_agent.py` - Add callback passing
2. `apps/zerg/backend/zerg/managers/agent_runner.py` - Ensure context is set (already done)

### Frontend
1. `apps/zerg/frontend-web/src/lib/useWebSocket.tsx` - Add streaming message handler
2. `apps/zerg/frontend-web/src/pages/ChatPage.tsx` - Add subscription and streaming UI

## Configuration

### Environment Variables
- `LLM_TOKEN_STREAM`: Set to `true`/`1`/`yes` to enable token streaming

### Backend Settings
- Check `apps/zerg/backend/zerg/config/__init__.py` for `llm_token_stream` setting

## Known Issues & Challenges

1. **LangChain API Complexity**: 
   - `ainvoke()` vs `astream()` for streaming
   - Callback passing varies by LangChain version
   - Need to test actual behavior

2. **Message Accumulation**:
   - With `astream()`, need to combine chunks into final AIMessage
   - Current code expects single AIMessage return

3. **Race Conditions**:
   - Frontend must handle messages arriving out of order
   - Optimistic UI vs final API state

4. **Tool Calls During Streaming**:
   - Tool outputs arrive as separate chunks
   - Need to associate with correct assistant message

## Next Steps

1. **Immediate**: Fix callback passing in `_call_model_async`
2. **Short-term**: Implement frontend subscription and streaming UI
3. **Medium-term**: Add comprehensive tests
4. **Long-term**: Optimize for high-frequency token delivery

## References

- LangChain Streaming: https://python.langchain.com/docs/modules/callbacks/
- WebSocket Protocol: `asyncapi/chat.yml`
- Backend Streaming: `apps/zerg/backend/zerg/routers/threads.py:250`
- Token Callback: `apps/zerg/backend/zerg/callbacks/token_stream.py`

