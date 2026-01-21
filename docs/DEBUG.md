# Debugging Guide

Three-layer debugging infrastructure for investigating LLM behavior in supervisor/worker runs.

**Note**: The supervisor/worker path is **LangGraph-free** (as of 2026-01-13). The ReAct loop runs in `supervisor_react_engine.py`. LangGraph is only used for the workflow engine.

## Quick Reference

| Question | Command |
|----------|---------|
| Debug trace end-to-end | `make debug-trace TRACE=<uuid>` |
| List recent traces | `make debug-trace RECENT=1` |
| Trace coverage report | `make trace-coverage` |
| View thread messages | `make debug-thread THREAD_ID=1` |
| Validate message integrity | `make debug-validate THREAD_ID=1` |
| Inspect workflow checkpoint | `make debug-inspect THREAD_ID=1` |
| View LLM interactions | `uv run python scripts/debug_run_audit.py --run-id 82` |
| Replay with mocked tools | `uv run python scripts/replay_run.py <run_id>` |
| List/trigger scheduled jobs | `curl localhost:30080/api/jobs/` |

## Trace-Centric Debugging (Recommended)

Every supervisor run gets a `trace_id` (UUID) that propagates through workers and LLM audit logs.

**Copy from UI**: In dev mode, trace_id appears in bottom-right corner of chat UI. Click to copy.

```bash
# Unified timeline
make debug-trace TRACE=abc-123-def

# Full details (LLM messages, tool calls)
make debug-trace TRACE=abc-123-def LEVEL=full

# Just errors and anomalies
make debug-trace TRACE=abc-123-def LEVEL=errors

# JSON output for AI agents
make debug-trace TRACE=abc-123-def --json
```

**What it shows:**
- Unified timeline across supervisor runs, workers, and LLM calls
- Duration and token usage per phase
- Anomaly detection (failed workers, slow LLM calls, stuck workers)

### MCP Tool for AI Agents

AI agents can debug traces via the `debug-trace` MCP server:

```json
{
  "debug-trace": {
    "command": "uv",
    "args": ["run", "python", "scripts/mcp_debug_trace/server.py"],
    "cwd": "/path/to/zerg",
    "transport": "stdio"
  }
}
```

Tools: `debug_trace(trace_id, level)`, `list_recent_traces(limit)`

## Layer 1: Thread Inspector

Inspect DB state (ThreadMessage table) and workflow checkpoints.

```bash
cd apps/zerg/backend

# View messages (compact JSON)
make debug-thread THREAD_ID=1

# Validate integrity (duplicates, ordering, tool response counts)
make debug-validate THREAD_ID=1

# Inspect workflow checkpoint state
make debug-inspect THREAD_ID=1

# Batch queries (minimal tokens)
echo '{"queries":[{"op":"thread","thread_id":1,"limit":5}]}' | make debug-batch
```

**Validation rules:**
- No duplicate messages (same role + content)
- Messages ordered by sent_at
- Each AIMessage tool_call has exactly one ToolMessage response

## Layer 2: LLM Audit Log

Every LLM request/response stored in `llm_audit_log` table.

```bash
cd apps/zerg/backend

# View LLM interactions for a run
uv run python scripts/debug_run_audit.py --run-id 82

# Include full message arrays
uv run python scripts/debug_run_audit.py --run-id 82 --show-messages
```

**What's captured:** Full messages array, response content, tool_calls, token counts, duration, phase, model, correlation to run_id/worker_id/thread_id.

```sql
SELECT phase, model, message_count, duration_ms,
       LEFT(response_content, 100) as response_preview
FROM llm_audit_log WHERE run_id = 82 ORDER BY created_at;
```

## Layer 3: Replay Harness

Re-run a supervisor with mocked tool results to test prompt changes.

```bash
cd apps/zerg/backend

# List recent runs
uv run python scripts/replay_run.py --list-recent 20

# Dry run (preview)
uv run python scripts/replay_run.py <run_id> --dry-run

# Full replay (real LLM, mocked spawn_worker)
uv run python scripts/replay_run.py <run_id>

# With options
uv run python scripts/replay_run.py <run_id> --match-threshold 0.8
```

**What's mocked:** `spawn_worker` returns cached results
**What's real:** LLM calls (that's what you're testing)
**Safe by default:** Unsafe tools blocked unless `--allow-all-tools`

## Debugging Workflow

1. **Get run_id** from logs or dashboard
2. **Check thread state** → `make debug-validate THREAD_ID=<id>`
3. **View LLM interactions** → `scripts/debug_run_audit.py --run-id <id>`
4. **Reproduce locally** → `scripts/replay_run.py <id>`
5. **Fix prompt** → Edit supervisor/worker prompt, replay to verify

## Frontend Logging Modes

Control console verbosity via URL parameter `?log=<level>`:

| Mode | Behavior |
|------|----------|
| `minimal` | Errors/warnings only |
| `normal` | Errors + key events (default) |
| `verbose` | Everything |
| `timeline` | Performance timing only |

Example: `http://localhost:30080/chat?log=verbose`
