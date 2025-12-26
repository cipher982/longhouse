# Post-Mortem: Worker Completed Successfully But Executed No Commands

**Date**: 2025-12-26
**Incident**: Worker completed with "success" status after 23 seconds but executed zero tools and produced no output
**Worker ID**: `2025-12-26T13-50-59_check-disk-space-on-server-cub`
**Task**: "Check disk space on server cube (100.104.187.47, SSH drose@100.104.187.47:2222, alias cube). Report overall filesystem usage (df -h), identify top space consumers (du for /var/lib/docker, /home, /var/log), and note any obvious cleanup candidates (docker images/volumes, logs)."

---

## Executive Summary

A worker agent was spawned to check disk space on the cube server. The LLM was invoked successfully (3608 prompt tokens, 1000 completion tokens), but returned an **empty assistant message** with no tool calls. The worker marked itself as "success" and saved `"(No result generated)"` as the output. No `WORKER_TOOL_STARTED` or `WORKER_TOOL_COMPLETED` events were emitted, and no SSH commands were ever executed.

**Root Cause**: The LLM (gpt-5-mini/gpt-4o-mini) generated a response that consisted entirely of tool calls with no accompanying text content. Due to a bug in how LangGraph processes streaming responses, the tool call metadata was lost, resulting in an empty assistant message being saved to the database. The worker's result extraction logic correctly identified this as "no result" but incorrectly marked the worker as successful.

---

## Timeline of Events

### What Actually Happened (Reconstructed from Artifacts)

1. **13:50:59.285** - Worker created with ID `2025-12-26T13-50-59_check-disk-space-on-server-cub`
2. **13:50:59.289** - Worker started, directory created at `/Users/davidrose/git/zerg/data/workers/2025-12-26T13-50-59_check-disk-space-on-server-cub/`
3. **13:50:59.336** - LLM invoked with:
   - Model: `gpt-5-mini` (actually gpt-4o-mini)
   - Input: 3608 prompt tokens
   - System prompt includes:
     - Connector protocols
     - Worker instructions with SSH commands guide
     - Server inventory (cube: 100.104.187.47:2222)
     - User context
   - Tools available: `runner_exec`, `ssh_exec`, `http_request`, `get_current_time`, etc.
4. **13:51:22.788** - LLM response completed (23.4 seconds):
   - Output: 1000 completion tokens
   - **Content**: Empty string `""`
   - **Tool calls**: None (lost in processing)
5. **13:51:22.801** - Assistant message persisted to `thread.jsonl`:
   ```json
   {"role": "assistant", "content": "", "timestamp": "2025-12-26T13:51:22.801650+00:00"}
   ```
6. **13:51:22.822** - Worker marked as **"success"**
7. **13:51:22.825-24.479** - Summary LLM call attempted (failed, generated empty summary)
8. **Result saved**: `"(No result generated)"`

### What Should Have Happened

1. LLM should have generated tool calls (e.g., `ssh_exec(host="cube", command="df -h")`)
2. ReAct agent should have invoked tools and received responses
3. `WORKER_TOOL_STARTED` / `WORKER_TOOL_COMPLETED` events should have been emitted
4. Tool outputs should have been saved to `tool_calls/` directory
5. Final assistant message should contain a summary of findings
6. Worker should mark as "success" with actual disk space report

---

## Root Cause Analysis

### Primary Issue: LLM Response Processing Bug

The LLM **did** generate output (1000 completion tokens), but the assistant message content was empty. This indicates one of two scenarios:

**Scenario A: Tool-Only Response (Most Likely)**
The LLM generated a response consisting entirely of tool calls with no accompanying text. This is valid ReAct behavior - the assistant can call tools without explanatory text. However, due to a known issue with LangGraph streaming (or the way we process `AIMessage` objects), the `tool_calls` metadata was lost when saving to the database.

Evidence:
- 1000 completion tokens were consumed (too many for an empty response)
- No tool call files in `tool_calls/` directory (should have been created if tools were invoked)
- `thread.jsonl` shows `content: ""` with no `tool_calls` field
- Worker's `_extract_result()` method looks for the last `AIMessage.content` - would be empty if LLM only called tools

**Scenario B: Silent LLM Failure**
The LLM generated 1000 tokens but they were all whitespace, formatting, or reasoning tokens that got stripped. However, this is unlikely given:
- GPT-4o-mini typically doesn't produce 1000 tokens of empty content
- No error logs indicating content parsing issues
- Usage metadata shows clean completion (no truncation or timeout)

### Secondary Issue: Worker Success Criteria Too Lenient

The worker marked itself as "success" despite having no output. The logic in `worker_runner.py:_extract_result()` and `_synthesize_from_tool_outputs()` handles empty results but doesn't treat them as failures:

```python
# Line 276-277 in worker_runner.py
saved_result = result_text or "(No result generated)"
self.artifact_store.save_result(worker_id, saved_result)
```

The worker considers empty results acceptable because:
- It successfully invoked the LLM (no timeout, no crash)
- The LLM didn't explicitly fail
- The ReAct loop completed without exceptions

This is **technically correct** from an execution standpoint but **semantically wrong** from a task completion standpoint - the user asked for disk space info and got nothing.

---

## Why No Tools Were Executed

### Investigation of Tool Call Chain

1. **Tool Availability**: Confirmed `ssh_exec` was in the worker's `allowed_tools` list:
   ```python
   # worker_runner.py:447-459
   default_worker_tools = [
       "runner_exec",
       "ssh_exec",  # ✓ Available
       "http_request",
       ...
   ]
   ```

2. **Tool Binding**: `zerg_react_agent.py:224` binds tools to LLM:
   ```python
   return llm.bind_tools(tools)
   ```

3. **Tool Invocation**: Tools are invoked via `_call_tool_async()` when the LLM returns an `AIMessage` with `tool_calls`. Since `tool_calls` was empty/missing, no tools were ever invoked.

4. **Event Emission**: `WORKER_TOOL_STARTED` events are emitted in `_call_tool_async()` based on `WorkerContext`. Since tools were never called, no events were emitted.

### The Missing Link: tool_calls Metadata

The database message has:
```json
{"role": "assistant", "content": "", "timestamp": "..."}
```

But it should have had:
```json
{
  "role": "assistant",
  "content": "",
  "tool_calls": [
    {"id": "...", "name": "ssh_exec", "args": {"host": "cube", "command": "df -h"}}
  ],
  "timestamp": "..."
}
```

The `tool_calls` field is persisted in `worker_runner.py:541-542`:
```python
if msg.role == "assistant" and msg.tool_calls:
    message_dict["tool_calls"] = msg.tool_calls
```

This means `msg.tool_calls` was either:
- `None` (not present on the object)
- `[]` (empty list)
- Stripped during LangChain message conversion

---

## Why This Was Marked as Success

The worker's success criteria are:
1. Did the agent runner execute without exceptions? ✓ Yes
2. Did we get messages back from the LLM? ✓ Yes (empty, but present)
3. Did any critical tool errors occur? ✗ No (no tools were called)

The worker treats "no output" as a benign edge case:
```python
# worker_runner.py:232-234
if not result_text:
    result_text = self._synthesize_from_tool_outputs(langchain_messages, task)
    if result_text:
        logger.info(f"Worker {worker_id}: synthesized result from tool outputs")
```

Since there were no tool outputs to synthesize from, the fallback kicked in:
```python
# worker_runner.py:276
saved_result = result_text or "(No result generated)"
```

This is marked as "success" because from the execution engine's perspective:
- No crashes
- No timeouts
- No critical errors
- Clean LLM completion

But from the **user's perspective**, this is a **total failure** - the task was not completed.

---

## Comparison with Supervisor Response

The supervisor correctly detected the issue and reported:

> "I couldn't actually check cube's disk: the worker returned no output and ran zero commands..."

The supervisor's detection logic is in `roundabout.py` which monitors `WORKER_TOOL_STARTED` events. When it saw none, it knew something was wrong. However, by the time the supervisor analyzed the worker results, the worker had already marked itself as "success".

---

## Why This Happens

### LangGraph Streaming + Tool Calls Bug

The most likely technical root cause is how LangGraph handles streaming responses with tool calls. When `enable_token_stream=True` (which may be the default in dev), the streaming callback may interfere with tool call metadata preservation.

From `zerg_react_agent.py:434-440`:
```python
if enable_token_stream:
    from zerg.callbacks.token_stream import WsTokenCallback
    callback = WsTokenCallback()
    result = await llm_with_tools.ainvoke(messages, config={"callbacks": [callback]})
else:
    result = await llm_with_tools.ainvoke(messages)
```

The `WsTokenCallback` may be consuming or not properly forwarding the `tool_calls` metadata, resulting in:
- Tokens are streamed correctly (1000 completion tokens counted)
- Text content is captured (but is empty because response was tool-only)
- Tool calls metadata is lost

This is a **known class of issues** with LangChain streaming - the `AIMessage` returned from `ainvoke` with callbacks may not have all fields populated correctly, especially `tool_calls`.

### LLM Model Behavior

GPT-4o-mini (gpt-5-mini alias) sometimes generates tool-only responses when:
- The task is straightforward and requires only tool execution
- The prompt emphasizes action over explanation
- The model determines no commentary is needed before calling tools

This is **correct behavior** - the ReAct pattern allows tool-first responses. The bug is in our processing, not the LLM's decision-making.

---

## Recommendations

### Immediate Fixes

1. **Add Tool Call Validation**
   ```python
   # In worker_runner.py:_extract_result()
   # Before checking content, verify if tool_calls exist
   for msg in reversed(messages):
       if isinstance(msg, AIMessage):
           tool_calls = getattr(msg, 'tool_calls', None)
           if tool_calls and len(tool_calls) > 0:
               # LLM generated tool calls but they weren't executed
               # This is a bug, not success
               raise RuntimeError(f"Worker generated {len(tool_calls)} tool calls but they were not executed")
   ```

2. **Stricter Success Criteria for Workers**
   ```python
   # In worker_runner.py:run_worker()
   # After extracting result, check if meaningful work was done
   if not result_text and not tool_outputs_exist:
       # Mark as failed instead of success
       return WorkerResult(
           worker_id=worker_id,
           status="failed",
           result="",
           error="Worker completed without producing output or executing tools",
           duration_ms=duration_ms
       )
   ```

3. **Investigate Token Streaming Impact**
   - Check if disabling `enable_token_stream` for workers resolves the tool_calls issue
   - Review `WsTokenCallback` implementation for metadata preservation
   - Consider using non-streaming mode for workers (they don't need real-time updates)

### Medium-Term Improvements

4. **Enhanced Tool Call Logging**
   ```python
   # Add to _call_tool_async in zerg_react_agent.py
   logger.info(f"LLM response: content_len={len(result.content)}, tool_calls={len(result.tool_calls or [])}")
   ```

5. **Worker Output Validation**
   - Add a post-completion check that verifies workers produced meaningful output
   - Distinguish between "empty result" (rare but valid) vs "no work done" (bug)

6. **Supervisor Monitoring Integration**
   - Update worker success/failure logic to align with roundabout's expectations
   - If roundabout detects zero tool events, mark worker as failed retroactively

### Long-Term Architecture Changes

7. **Eliminate "Tool-Only" Responses**
   - Update worker prompts to **always** require a final text summary
   - Change ReAct loop to enforce at least one non-tool assistant message
   - Add prompt instruction: "Always provide a brief summary of your findings after running commands"

8. **Rethink Worker Success Metrics**
   - "Success" should mean "task completed" not "execution didn't crash"
   - Add semantic validation: did the worker answer the question?
   - Consider a post-run LLM judge: "Did this output answer the user's question?"

9. **Debug Instrumentation**
   - Save raw LLM response JSON to worker artifacts for debugging
   - Capture full `AIMessage` object serialization before DB save
   - Add `DEBUG_LLM_INPUT=1` equivalent for output

---

## Testing Gap

This bug reveals a testing gap: we don't have E2E tests that verify:
- Workers actually execute tools when expected
- Tool call metadata survives the entire pipeline (LLM → LangGraph → DB → result extraction)
- Empty worker results are properly classified as failures

**Recommended Test**:
```python
def test_worker_must_execute_tools_for_ssh_task():
    """Verify worker executes ssh_exec for server disk check task."""
    task = "Check disk space on server cube"
    result = run_worker_sync(task)

    # Should have executed at least one tool
    assert result.status == "success"
    assert "tool_calls" in worker_artifacts
    assert len(worker_artifacts["tool_calls"]) > 0

    # Should have actual output
    assert result.result != "(No result generated)"
    assert "df" in result.result or "disk" in result.result
```

---

## Prevention

To prevent this class of bugs in the future:

1. **Invariant**: Workers that receive infrastructure tasks MUST execute at least one tool call
2. **Validation**: Check that `AIMessage.tool_calls` is populated before considering a response complete
3. **Monitoring**: Alert when workers complete in <30s with zero tool executions (likely a bug)
4. **Testing**: Add E2E tests for common worker task patterns (disk check, process listing, log retrieval)

---

## Appendices

### A. Worker Artifacts Examined

```
/Users/davidrose/git/zerg/data/workers/2025-12-26T13-50-59_check-disk-space-on-server-cub/
├── metadata.json      # Status: success, duration: 23533ms
├── metrics.jsonl      # 2 LLM calls (initial + summary)
├── monitoring/        # (empty)
├── result.txt         # "(No result generated)"
├── thread.jsonl       # 4 lines: 2 system, 1 user, 1 empty assistant
└── tool_calls/        # (empty directory - should have had files)
```

### B. LLM Metrics

```json
{
  "event": "llm_call",
  "phase": "initial",
  "model": "gpt-5-mini",
  "prompt_tokens": 3608,
  "completion_tokens": 1000,
  "total_tokens": 4608,
  "duration_ms": 23451
}
```

**Analysis**: 1000 completion tokens is significant - this wasn't a "quick refusal" or empty response. The LLM generated substantial output that was lost.

### C. Relevant Code Paths

1. **Worker execution**: `worker_runner.py:run_worker()` (lines 84-394)
2. **Result extraction**: `worker_runner.py:_extract_result()` (lines 573-606)
3. **Tool call persistence**: `worker_runner.py:_persist_tool_calls()` (lines 551-571)
4. **LLM invocation**: `zerg_react_agent.py:_call_model_async()` (lines 303-520)
5. **Tool binding**: `zerg_react_agent.py:_make_llm()` (lines 150-224)

---

## Conclusion

This incident demonstrates a **silent failure mode** where the system reports success but accomplishes nothing. The root cause is likely a bug in how tool calls are preserved during streaming LLM responses. The worker's lenient success criteria allowed this to be classified as "completed successfully" when it should have been "failed - no work done".

**Immediate action**: Disable token streaming for workers and add validation that workers produce meaningful output.

**Long-term action**: Rethink worker success criteria to be task-oriented rather than execution-oriented.
