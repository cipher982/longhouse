# Jarvis Chat Performance Analysis

> Investigation conducted 2025-12-27. Reference for future optimization work.

## Executive Summary

Jarvis chat streaming was perceived as slow compared to ChatGPT. Investigation revealed:

- **Streaming is healthy**: Backend delivers tokens at ~45 tok/s (normal for gpt-5.2)
- **Perceived slowness is TTFT**: ~7,400 base tokens must be processed before first output
- **Tools are the biggest cost**: 18 tools consume 5,046 tokens (69% of base prompt)
- **No backend bottleneck**: The 2x difference between SSE (45 tok/s) and E2E measurements (24.7 tok/s) is due to DOM rendering overhead, not streaming issues

## Token Breakdown

### Base Prompt Composition

| Component | Tokens | % of Base | Notes |
|-----------|--------|-----------|-------|
| **System Instructions** | 2,137 | 29% | Agent personality, rules, capabilities |
| **Tools (18 total)** | 5,046 | 69% | JSON schemas for each tool |
| **Agent Context** | 182 | 2% | Connector status, user context |
| **BASE TOTAL** | **~7,365** | 100% | Before history or user message |

### Tool Token Costs (Sorted)

| Tool | Tokens | % of Tools | Notes |
|------|--------|------------|-------|
| `send_email` | 831 | 16.5% | Complex schema with recipients, attachments |
| `web_search` | 797 | 15.8% | Many parameters for search options |
| `get_whoop_data` | 467 | 9.3% | Health metrics schema |
| `http_request` | 418 | 8.3% | Generic HTTP with headers, body, etc. |
| `contact_user` | 380 | 7.5% | Multi-channel contact options |
| `search_notes` | 367 | 7.3% | Obsidian search parameters |
| `get_current_location` | 332 | 6.6% | Location with options |
| `web_fetch` | 239 | 4.7% | URL fetching |
| `spawn_worker` | 220 | 4.4% | Worker delegation |
| `knowledge_search` | 164 | 3.3% | RAG search |
| `list_workers` | 161 | 3.2% | Worker listing |
| `read_worker_file` | 132 | 2.6% | File reading |
| `grep_workers` | 116 | 2.3% | Worker grep |
| `runner_create_enroll_token` | 113 | 2.2% | Runner enrollment |
| `get_worker_metadata` | 95 | 1.9% | Worker metadata |
| `read_worker_result` | 88 | 1.7% | Result reading |
| `runner_list` | 71 | 1.4% | Runner listing |
| `get_current_time` | 55 | 1.1% | Simple time tool |

**Key insight**: Top 3 tools (`send_email`, `web_search`, `get_whoop_data`) consume 2,095 tokens (41% of all tool tokens).

## Streaming Performance

### Measurement Layers

| Layer | Rate | Method |
|-------|------|--------|
| Raw LangChain | 58-74 tok/s | Direct `ChatOpenAI.ainvoke()` in container |
| SSE HTTP | 44.7 tok/s | `curl` to `/api/jarvis/chat` |
| E2E Playwright | 24.7 tok/s | DOM polling via `waitForFunction` |

### Why E2E Shows Lower Throughput

The ~2x difference between SSE (45 tok/s) and E2E (25 tok/s) is expected:

1. **React batches state updates** - Multiple tokens may render in a single frame
2. **Playwright polls every ~50ms** - Can't detect sub-50ms changes
3. **DOM measurement overhead** - Both `firstTokenAt` and `streamingCompleteAt` are DOM-based timestamps

This is **not a bug** - it reflects the actual perceived rendering speed in the browser.

## How to Replicate Tests

### 1. Token Breakdown Analysis

```bash
# Run inside the backend container
docker exec zerg-backend-1 python -c "
import tiktoken
from zerg.tools.unified_access import get_tool_resolver
from zerg import crud
from zerg.database import get_db_session
import json

enc = tiktoken.encoding_for_model('gpt-4')

def count_tokens(text):
    return len(enc.encode(str(text)))

with get_db_session() as db:
    agent = crud.get_agent(db, 1)

    # System instructions
    sys_tokens = count_tokens(agent.system_instructions or '')
    print(f'System Instructions: {sys_tokens:,} tokens')

    # Tools
    resolver = get_tool_resolver()
    tools = resolver.filter_by_allowlist(agent.allowed_tools)

    total_tool_tokens = 0
    for tool in tools:
        schema = tool.get_input_schema().schema() if hasattr(tool, 'get_input_schema') else {}
        tool_text = f'{tool.name}: {tool.description} {json.dumps(schema)}'
        tokens = count_tokens(tool_text)
        total_tool_tokens += tokens
        print(f'  {tool.name}: {tokens} tokens')

    print(f'Tools Total: {total_tool_tokens:,} tokens ({len(tools)} tools)')
"
```

### 2. SSE Streaming Rate Test

```bash
# Make a chat request and analyze token timing
curl -s -X POST "http://127.0.0.1:47300/api/jarvis/chat" \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"message": "Explain technical debt in exactly 100 words."}' \
  --max-time 60 > /tmp/sse_test.txt

# Count events (divide by 2 because grep matches both event: and data: lines)
echo "Token events: $(($(grep -c 'supervisor_token' /tmp/sse_test.txt) / 2))"

# Get completion tokens from usage
grep 'supervisor_complete' /tmp/sse_test.txt | grep -oE '"completion_tokens": [0-9]+'

# Get timestamps for rate calculation
grep 'event: supervisor_token' /tmp/sse_test.txt -A1 | grep timestamp | head -1
grep 'supervisor_complete' /tmp/sse_test.txt | grep -oE 'T[0-9:.]+'
```

### 3. Raw LangChain Streaming Test

```bash
# Test raw LLM streaming speed (bypasses SSE layer)
docker exec zerg-backend-1 python -c "
import asyncio
import time

async def test_rate():
    from langchain_openai import ChatOpenAI
    from langchain_core.callbacks.base import AsyncCallbackHandler

    token_times = []

    class TimingCallback(AsyncCallbackHandler):
        async def on_llm_new_token(self, token: str, **kwargs):
            token_times.append(time.perf_counter())

    llm = ChatOpenAI(model='gpt-5.2', streaming=True)

    print('Testing raw LangChain streaming...')
    start = time.perf_counter()

    await llm.ainvoke(
        'Explain technical debt in exactly 100 words.',
        config={'callbacks': [TimingCallback()]}
    )

    elapsed = time.perf_counter() - start

    if len(token_times) > 1:
        intervals = [token_times[i+1] - token_times[i] for i in range(len(token_times)-1)]
        avg_interval = sum(intervals) / len(intervals)
        tokens_per_sec = 1.0 / avg_interval if avg_interval > 0 else 0

        print(f'Total callbacks: {len(token_times)}')
        print(f'Total time: {elapsed:.2f}s')
        print(f'Callbacks/sec: {tokens_per_sec:.1f}')

asyncio.run(test_rate())
"
```

### 4. E2E Performance Tests

```bash
# Run the performance evaluation suite
make test-e2e-single TEST=tests/chat_performance_eval.spec.ts

# Or run with timeline logging for detailed output
cd apps/zerg/e2e && BACKEND_PORT=8001 FRONTEND_PORT=8002 bunx playwright test tests/chat_performance_eval.spec.ts
```

## Optimization Opportunities

### High Impact (Token Reduction)

1. **Reduce tool count** - Remove rarely-used tools from the supervisor's allowlist
   - Potential savings: 500-2000+ tokens per removed tool

2. **Compress tool schemas** - Simplify parameter descriptions, remove verbose examples
   - `send_email` alone could potentially be halved with tighter descriptions

3. **Lazy tool loading** - Only include tools relevant to the conversation context
   - Requires more complex tool resolution logic

4. **Shorter system prompt** - Review and trim system instructions
   - Current: 2,137 tokens (~9,400 chars)

### Medium Impact (Perceived Speed)

5. **Optimize TTFT** - First token latency is dominated by prompt processing
   - Smaller prompt = faster first token

6. **Token batching on frontend** - Batch React state updates for smoother rendering
   - Currently updates state per-token

### Low Impact (Already Optimized)

7. **SSE streaming** - Already delivers at model speed (~45 tok/s)
8. **Event bus** - Adds only ~0.08ms overhead per token
9. **WebSocket path** - Already removed for supervisor runs (was redundant)

## Changes Made During Investigation

### Kept: Skip WebSocket Broadcast for Supervisor Runs

File: `zerg/callbacks/token_stream.py`

```python
# For supervisor runs, SSE is the primary delivery path - skip WS broadcast
# to avoid redundant Pydantic serialization + lock contention overhead.
if run_id is not None:
    await event_bus.publish(EventType.SUPERVISOR_TOKEN, {...})
    return  # Early return - don't also broadcast via WebSocket
```

This eliminates unnecessary Pydantic serialization and lock acquisition for every token during Jarvis chat.

### Removed: Debug Scripts

Temporary scripts created during investigation were cleaned up:
- `zerg/debug_streaming.py` - Raw LangChain timing
- `zerg/debug_streaming_e2e.py` - SSE endpoint testing

## Comparison to ChatGPT

Why ChatGPT feels faster:

| Aspect | Jarvis | ChatGPT (estimated) |
|--------|--------|---------------------|
| Base prompt | ~7,400 tokens | ~1,000-2,000 tokens |
| Tools | 18 tools, full schemas | Fewer, server-compressed |
| TTFT | ~1-2 seconds | ~200-500ms |
| Streaming | ~45 tok/s | ~50-80 tok/s |
| Edge optimization | None | Global CDN, edge inference |

The primary difference is **prompt size affecting TTFT**, not streaming speed.

## Monitoring & Alerting

### Key Metrics to Track

1. **prompt_tokens** - Available in `supervisor_complete` event usage data
2. **TTFT** - Time from request to first `supervisor_token` event
3. **Streaming duration** - Time from first token to `supervisor_complete`
4. **completion_tokens / streaming_duration** - Effective throughput

### Thresholds (Suggested)

| Metric | Good | Acceptable | Investigate |
|--------|------|------------|-------------|
| TTFT | <1.5s | <2.5s | >3s |
| Throughput | >40 tok/s | >25 tok/s | <20 tok/s |
| Base prompt | <6k tokens | <8k tokens | >10k tokens |

## References

- E2E performance test: `apps/zerg/e2e/tests/chat_performance_eval.spec.ts`
- Token streaming callback: `apps/zerg/backend/zerg/callbacks/token_stream.py`
- SSE streaming: `apps/zerg/backend/zerg/routers/jarvis_sse.py`
- Tool resolver: `apps/zerg/backend/zerg/tools/unified_access.py`
- Agent runner (prompt assembly): `apps/zerg/backend/zerg/managers/agent_runner.py`
