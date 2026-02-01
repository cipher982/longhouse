# Prompt Cache Optimization Plan

**Status**: Analysis Complete
**Date**: 2026-01-31
**Author**: cache-planner agent

## Executive Summary

Zerg's `fiche_runner.py` builds prompts via `MessageArrayBuilder`, which **already follows good cache optimization patterns**. The current implementation places static content first and dynamic content last, maximizing prefix cache hits for both OpenAI and Anthropic APIs.

This document analyzes the current state and identifies incremental improvements.

## Current Message Ordering Analysis

### MessageArrayBuilder Layout (Current)

```
Position | Content Type              | Cache Status
---------|---------------------------|------------------
1        | SystemMessage             | STATIC (cacheable)
         |   - connector_protocols   |   - Static protocols
         |   - system_instructions   |   - Per-fiche, stable
         |   - skills_prompt         |   - Per-fiche, stable
---------|---------------------------|------------------
2-N      | Conversation History      | SEMI-STATIC (grows)
         |   - HumanMessage(s)       |   - Previous turns
         |   - AIMessage(s)          |   - Previous turns
         |   - ToolMessage(s)        |   - Previous turns
---------|---------------------------|------------------
N+1      | SystemMessage (dynamic)   | DYNAMIC (per-turn)
         |   - current_time          |   - Changes every request
         |   - connector_status      |   - Changes with integrations
         |   - memory_context        |   - Query-dependent RAG
```

### Key Files

| File | Purpose | Lines |
|------|---------|-------|
| `managers/message_array_builder.py` | Message array construction | 1-506 |
| `managers/fiche_runner.py` | Orchestrates runs | 188-225, 576-598 |
| `prompts/connector_protocols.py` | Static protocol definitions | 1-88 |
| `connectors/status_builder.py` | Dynamic context builder | 373-446 |

### What's Already Cache-Optimized

1. **Static protocols first**: `connector_protocols.py` content is static and injected at the start of the system prompt (line 147-148 in `message_array_builder.py`)

2. **Conversation extends prefix**: Conversation history comes after system prompt, meaning each turn extends the cacheable prefix

3. **Dynamic content last**: `with_dynamic_context()` explicitly appends at the end (line 306-311 in `message_array_builder.py`)

4. **Phase enforcement**: `BuildPhase` enum prevents accidental mis-ordering

## Cache-Busting Issues Identified

### Issue 1: Timestamp in Dynamic Context (HIGH IMPACT)

**Location**: `connectors/status_builder.py:411-443`

```python
current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
# ...
context = f"""<current_time>{current_time}</current_time>
<connector_status captured_at="{current_time}">
```

**Problem**: Timestamp changes every second, busting cache even when nothing else changed.

**Recommendation**: Move timestamp to a separate, final injection point OR only include date (not time) for cache stability.

### Issue 2: Memory Context Query Variance (MEDIUM IMPACT)

**Location**: `message_array_builder.py:445-505`

The memory context search results vary based on the user query, which is expected. However, the results are injected as part of the same `SystemMessage` as connector status.

**Problem**: If memory search returns different results (even slightly different ordering), the entire dynamic context block is considered different.

**Recommendation**: Consider caching memory search results for identical queries within a session, or separating memory context into its own message.

### Issue 3: Connector Status JSON Ordering (LOW IMPACT)

**Location**: `connectors/status_builder.py:442`

```python
json.dumps(connector_status, **json_kwargs)
```

**Problem**: Python dicts maintain insertion order (3.7+), but if connector registration order changes, JSON output changes.

**Recommendation**: Sort connector keys alphabetically for deterministic output:
```python
json.dumps(connector_status, sort_keys=True, **json_kwargs)
```

## Proposed Changes

### Phase 1: Quick Wins (Low Risk)

1. **Sort connector status keys**
   - File: `connectors/status_builder.py:434-436`
   - Change: Add `sort_keys=True` to `json_kwargs`
   - Risk: None
   - Effort: 5 minutes

2. **Stabilize timestamp granularity**
   - File: `connectors/status_builder.py:411`
   - Change: Use minute-level granularity: `strftime("%Y-%m-%dT%H:%MZ")`
   - Risk: Slight loss of temporal precision (acceptable for context)
   - Effort: 5 minutes

### Phase 2: Structural Improvements (Medium Risk)

3. **Separate dynamic context components**
   - File: `message_array_builder.py:306-311`
   - Change: Inject each dynamic component as separate `SystemMessage`:
     ```
     SystemMessage(content=temporal_context)   # time only
     SystemMessage(content=connector_status)    # connectors
     SystemMessage(content=memory_context)      # RAG results
     ```
   - Risk: Increases message count; verify LLM handles multiple system messages
   - Effort: 1-2 hours

4. **Add memory search caching**
   - File: `message_array_builder.py:462-485`
   - Change: Cache memory search results for (owner_id, query_hash) for 60s
   - Risk: Stale results for rapidly changing memory
   - Effort: 2-3 hours

### Phase 3: Advanced Optimization (Higher Risk)

5. **Implement cache-aware conversation windowing**
   - Context: Long conversations may exceed context limits, requiring truncation
   - Current: Not implemented (conversations are passed in full)
   - Proposed: Truncate from middle, keeping recent turns and initial context intact
   - Risk: Information loss; complex to implement correctly
   - Effort: 4-6 hours

## Implementation Order

1. **Phase 1** - Implement immediately (today)
2. **Phase 2** - Implement after cache hit metrics are established
3. **Phase 3** - Only if cache miss rates remain high after Phase 1-2

## Testing Strategy

### Cache Hit Verification

Add logging to track cache performance:

```python
# In oikos_react_engine.py after LLM call
if hasattr(response, 'usage'):
    cache_read = response.usage.get('cache_read_input_tokens', 0)
    cache_miss = response.usage.get('prompt_tokens', 0) - cache_read
    logger.info(f"Cache: read={cache_read}, miss={cache_miss}")
```

### Regression Testing

- Run existing E2E tests after each phase
- Monitor for:
  - Tool call failures (timestamp parsing)
  - Context injection errors
  - Message ordering issues

## Metrics to Track

| Metric | Current | Target | Method |
|--------|---------|--------|--------|
| Cache hit rate | Unknown | >80% | LLM usage metadata |
| Avg prompt tokens | Unknown | -20% | Token counting |
| Avg latency | Unknown | -15% | Request timing |

## Risks and Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Multiple SystemMessages confuse LLM | High | Test with each provider separately |
| Timestamp granularity too coarse | Low | Keep ISO format, just reduce precision |
| Memory caching returns stale data | Medium | Short TTL (60s), cache key includes query hash |

## Conclusion

The current implementation is **already well-optimized** for prompt caching. The primary opportunity is reducing timestamp precision to avoid per-second cache invalidation. Phase 1 changes can be implemented immediately with minimal risk.

---

## Appendix: Code Locations

### Primary Files

- `/Users/davidrose/git/zerg/apps/zerg/backend/zerg/managers/message_array_builder.py`
- `/Users/davidrose/git/zerg/apps/zerg/backend/zerg/managers/fiche_runner.py`
- `/Users/davidrose/git/zerg/apps/zerg/backend/zerg/connectors/status_builder.py`
- `/Users/davidrose/git/zerg/apps/zerg/backend/zerg/prompts/connector_protocols.py`

### Related Files

- `/Users/davidrose/git/zerg/apps/zerg/backend/zerg/services/oikos_react_engine.py` (LLM invocation)
- `/Users/davidrose/git/zerg/apps/zerg/backend/zerg/services/memory_search.py` (memory search)
