# Trace-Centric Debugging & Supervisor Hardening - Final Report

**Date:** 2026-01-14
**Status:** Complete
**Tests:** 1344 backend passed, 92 frontend passed

---

## Executive Summary

This session implemented two major features:

1. **Trace-Centric Debugging** - One ID to debug everything: copy `trace_id` from UI → query full context via CLI or AI agent
2. **Supervisor/Worker Hardening** - Bug fixes and design improvements identified by multi-agent code review (Codex + Gemini)

Combined with the earlier LangGraph removal work (see `2026-01-14-langgraph-removal-handoff.md`), the supervisor/worker system is now simpler, more debuggable, and more robust.

---

## Part 1: Trace-Centric Debugging

### Problem Solved

Debugging was painful:
- Multiple scattered IDs (run_id, thread_id, worker_job_id, message_id)
- No single ID to grab when a bug happens
- Docker logs are ephemeral
- Hard for AI agents to query and understand issues

### Solution

**One ID to rule them all**: `trace_id` is a UUID generated at request entry and propagated to all related records.

```
User copies trace_id from UI footer
    ↓
CLI: make debug-trace TRACE=abc-123
    ↓
Shows unified timeline: supervisor → worker → LLM calls
```

### Implementation

#### Schema Changes (Phase 0)
| Table | Column Added |
|-------|--------------|
| `agent_runs` | `trace_id UUID` (indexed) |
| `worker_jobs` | `trace_id UUID` (indexed) |
| `llm_audit_log` | `trace_id UUID`, `span_id UUID` (indexed) |

#### Context Propagation (Phases 1-2)
```
jarvis_chat.py (generate trace_id)
    ↓
SupervisorContext (trace_id field added)
    ↓
supervisor_tools.py (copy to WorkerJob)
    ↓
WorkerContext (trace_id field added)
    ↓
llm_audit.py (include in audit records)
```

#### Debug Script (Phase 3)
`apps/zerg/backend/scripts/debug_trace.py`:
- Queries all tables by trace_id
- Produces unified timeline
- Supports `--level summary|full|errors`
- JSON output for programmatic use

```bash
# CLI usage
make debug-trace TRACE=abc-123

# Example output
Trace: 75b705d5-c008-4081-8e16-904f6d2c37d0
Started: 2026-01-14 19:52:01 UTC
Duration: 20.36s
Status: SUCCESS
Runs: 1 | Workers: 1 | LLM calls: 4

Timeline:
────────────────────────────────────────────────────────────
19:52:01.239  supervisor.run.started   run_id=120
19:52:01.255  worker.spawned           job_id=74 task=Check if nginx...
19:52:01.296  llm.generate             model=gpt-5.2 tokens=6227/60
19:52:04.897  worker.started           worker_id=2026-01-14T19-52-04...
19:52:15.286  worker.success           duration_ms=10389
19:52:21.598  supervisor.run.success   total_tokens=6447
```

#### UI Integration (Phase 4)
- `TraceIdDisplay.tsx` - Footer badge with copy button
- SSE events include `trace_id` in payload
- `event-bus.ts` types updated for traceId

#### MCP Server (Phase 5)
`scripts/mcp_debug_trace/server.py`:
- Exposes `debug_trace` and `list_recent_traces` tools
- Allows AI agents to query traces programmatically
- STDIO transport for IDE integration

### Files Created/Modified

**New Files:**
- `apps/zerg/backend/scripts/debug_trace.py` (14KB)
- `scripts/mcp_debug_trace/server.py` (9KB)
- `apps/zerg/frontend-web/src/jarvis/app/components/TraceIdDisplay.tsx` (4KB)

**Modified Files:**
- `zerg/models/run.py` - Added trace_id column
- `zerg/models/models.py` - Added trace_id to WorkerJob
- `zerg/models/llm_audit.py` - Added trace_id, span_id columns
- `zerg/context.py` - Added trace_id to WorkerContext
- `zerg/services/supervisor_context.py` - Added trace_id to SupervisorContext
- `zerg/routers/jarvis_chat.py` - Generate trace_id at entry
- `zerg/services/supervisor_service.py` - Set trace_id on context
- `zerg/tools/builtin/supervisor_tools.py` - Copy trace_id to WorkerJob
- `zerg/services/worker_resume.py` - Inherit trace_id on continuation
- `zerg/services/worker_runner.py` - Set trace_id on WorkerContext
- `zerg/services/worker_job_processor.py` - Pass trace_id in event_context
- `zerg/services/llm_audit.py` - Store trace_id/span_id, add TTL pruning
- `zerg/managers/agent_runner.py` - Check WorkerContext for trace_id
- `schemas/sse-events.asyncapi.yml` - Added traceId to event schemas
- Frontend: `event-bus.ts`, `supervisor-chat-controller.ts`, `App.tsx`

---

## Part 2: Supervisor/Worker Hardening

### Multi-Agent Code Review

Codex and Gemini reviewed the LangGraph-free supervisor implementation and identified issues.

### Bug Fixes (5)

| Issue | Severity | File | Fix |
|-------|----------|------|-----|
| **Token accounting on WAITING** | Medium | `supervisor_service.py:674` | Persist `run.total_tokens` before setting WAITING status |
| **Continuation re-interrupt tokens** | Medium | `worker_resume.py:287` | Add partial tokens when continuation spawns another worker |
| **Orphaned ToolMessage** | Medium | `agent_runner.py:510-541` | Find parent assistant message, pass `parent_id` to `save_new_messages` |
| **Zero tokens check** | Low | `supervisor_service.py:774` | Changed `if runner.usage_total_tokens:` to `is not None` |
| **LLM audit memory leak** | Low | `llm_audit.py:281-296` | Added TTL prune (10 min) for stale pending entries |

### Design Fixes (2)

| Issue | File | Fix |
|-------|------|-----|
| **Unsafe prefix idempotency** | `supervisor_tools.py` | Removed prefix matching - exact task/tool_call_id only |
| **spawn_worker + other tools** | `supervisor_react_engine.py:722-755` | Enforce spawn_worker must be solo; return error if mixed |

#### Prefix Matching Removal

**Before:** spawn_worker used prefix containment to match "rephrased" tasks:
```python
# "Check disk space on cube" would match "Check disk space on cube real quick"
prefix_matches = [j for j in jobs if task.startswith(j.task[:50])]
```

**Problem:** Near-matches could return wrong worker results if tasks share prefixes.

**After:** Exact matching only:
```python
# Only exact task match or tool_call_id match
for job in completed_jobs:
    if job.task == task:  # Exact match
        existing_job = job
        break
```

#### spawn_worker Solo Enforcement

**Before:** LLM could call spawn_worker with other tools in the same turn:
```
AIMessage: tool_calls=[get_time(), spawn_worker("task"), send_email()]
```

**Problem:** Tools before spawn_worker run, then interrupt; tools after only run on resume. Confusing execution order.

**After:** Runtime guard rejects mixed tool sets:
```python
if has_spawn_worker and len(tool_calls) > 1:
    # Return error ToolMessage for each call
    error_msg = "Error: spawn_worker must be called alone..."
    # Continue loop to let LLM correct itself
```

### Test Updates

| Old Test | New Test | Change |
|----------|----------|--------|
| `test_prefix_match_for_completed_jobs` | (deleted) | Tested removed feature |
| `test_no_prefix_collision...` | `test_no_fuzzy_matching_for_similar_tasks` | Updated description |
| `test_double_worker_spawn_on_resume_bug` | `test_different_tasks_create_separate_workers` | Expects 2 workers (new behavior) |

---

## Combined Architecture

After all changes, the supervisor/worker flow:

```
User Message → jarvis_chat.py
    │
    ├── Generate trace_id (UUID)
    ├── Create AgentRun with trace_id
    │
    ▼
SupervisorService.start_run()
    │
    ├── Set SupervisorContext (run_id, owner_id, message_id, trace_id)
    │
    ▼
AgentRunner.run_thread()
    │
    ▼
supervisor_react_engine.run_supervisor_loop()
    │
    ├── Check: spawn_worker must be solo tool call (NEW)
    │
    ├── LLM call → log to llm_audit with trace_id/span_id
    │
    ├── Tool execution
    │   └── spawn_worker? → Copy trace_id to WorkerJob
    │                       Raises AgentInterrupted
    │
    └── Persist tokens on WAITING (NEW)

[If interrupted]
    │
    ▼
Worker executes
    │
    ├── Set WorkerContext with trace_id (NEW)
    ├── LLM calls logged with trace_id
    │
    ▼
worker_resume.resume_supervisor()
    │
    ├── Inherit trace_id from original run
    │
    ▼
AgentRunner.run_continuation()
    │
    ├── Find parent message for ToolMessage (NEW)
    ├── Persist tokens on re-interrupt (NEW)
    │
    ▼
Final response with correct total_tokens
```

---

## Verification Completed

### Live Testing
1. ✅ Database columns exist (trace_id in all 3 tables, span_id in llm_audit_log)
2. ✅ UI trace badge appears with truncated ID
3. ✅ Click to copy works
4. ✅ trace_id updates on new run
5. ✅ Worker inherits same trace_id as supervisor
6. ✅ Debug script shows unified timeline
7. ✅ MCP server responds to tools/list and debug_trace

### Test Results
- **Backend:** 1344 passed, 30 skipped
- **Frontend:** 92 passed, 6 skipped

---

## Files Changed Summary

### New Files (3)
```
apps/zerg/backend/scripts/debug_trace.py          # CLI debug tool
scripts/mcp_debug_trace/server.py                  # MCP server
apps/zerg/frontend-web/.../TraceIdDisplay.tsx      # UI component
```

### Modified Files (26)
```
# Backend - Models
zerg/models/run.py                                 # trace_id column
zerg/models/models.py                              # WorkerJob.trace_id
zerg/models/llm_audit.py                           # trace_id, span_id

# Backend - Context
zerg/context.py                                    # WorkerContext.trace_id
zerg/services/supervisor_context.py                # SupervisorContext.trace_id

# Backend - Services
zerg/services/supervisor_service.py                # trace_id propagation, token fix
zerg/services/supervisor_react_engine.py           # spawn_worker solo guard
zerg/services/worker_resume.py                     # trace_id inheritance, token fix
zerg/services/worker_runner.py                     # WorkerContext.trace_id
zerg/services/worker_job_processor.py              # trace_id in event_context
zerg/services/llm_audit.py                         # trace_id storage, TTL prune
zerg/managers/agent_runner.py                      # parent_id, WorkerContext fallback
zerg/routers/jarvis_chat.py                        # trace_id generation

# Backend - Tools
zerg/tools/builtin/supervisor_tools.py             # trace_id copy, prefix removal

# Backend - Tests
tests/test_spawn_worker_idempotency.py             # Updated for exact matching
tests/test_master_worker_flow_integration.py       # Updated expectations
tests/test_langgraph_free_resume.py                # Minor update

# Frontend
src/jarvis/app/App.tsx                             # TraceIdDisplay import
src/jarvis/app/components/index.ts                 # Export
src/jarvis/lib/event-bus.ts                        # traceId types
src/jarvis/lib/supervisor-chat-controller.ts       # traceId emission
src/generated/sse-events.ts                        # Generated

# Schemas
schemas/sse-events.asyncapi.yml                    # traceId fields

# Docs
AGENTS.md                                          # Debug commands
Makefile                                           # debug-trace target
```

---

## Debug Commands Reference

```bash
# Trace debugging
make debug-trace TRACE=<uuid>              # Full timeline
make debug-trace TRACE=<uuid> LEVEL=full   # With LLM details
make debug-trace TRACE=<uuid> LEVEL=errors # Anomalies only

# LangGraph debugging (still available)
make debug-thread THREAD_ID=1              # View thread messages
make debug-validate THREAD_ID=1            # Check message integrity

# LLM audit
cd apps/zerg/backend
uv run python scripts/debug_run_audit.py --run-id 82

# Replay (for prompt testing)
uv run python scripts/replay_run.py <run_id>
```

---

## Future Considerations

These were identified but not addressed (out of scope):

| Item | Notes |
|------|-------|
| **OpenTelemetry export** | Could export traces to Jaeger/Honeycomb |
| **Trace UI** | Visual timeline in dashboard |
| **Anomaly detection** | Auto-detect common issues |
| **Evidence mounting** | `EvidenceMountingLLM` wrapper not used in new engine |
| **React key warnings** | Console spam from UI (non-blocking) |

---

## Related Documents

| Document | Purpose |
|----------|---------|
| `docs/handoffs/2026-01-14-langgraph-removal-handoff.md` | LangGraph removal details |
| `docs/specs/durable-runs-v2.2.md` | Architecture spec |
| `~/.claude/plans/logical-hugging-origami.md` | Trace debugging plan |
| `AGENTS.md` (Debug Pipeline section) | Debug tools reference |

---

## Sign-off

All planned work complete:
- ✅ Trace-centric debugging (6 phases)
- ✅ Multi-agent review fixes (7 issues)
- ✅ Tests passing
- ✅ Live verification
- ✅ Documentation updated

The supervisor/worker system is now production-ready with comprehensive debugging capabilities.
