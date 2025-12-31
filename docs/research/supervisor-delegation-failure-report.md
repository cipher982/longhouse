# Research Report: Supervisor Delegation Logic Failures

**Date:** 2025-12-31
**Subject:** Investigation into `delegation_task_clarity` live eval failure

## Executive Summary

The investigation into the 0% score for conditional delegation tasks revealed multiple systemic issues ranging from prompt over-optimization to evaluation-specific deadlocks. While the worker is capable of executing complex conditional commands, the supervisor is currently blind to the outcomes in evaluation mode, and the worker is discouraged from multi-step reasoning by its system prompt.

## Key Findings

### 1. Worker Prompt Over-Optimization
The `BASE_WORKER_PROMPT` contains a heavy emphasis on "ONE command, then STOP".
- **Impact:** While efficient for 90% of simple tasks (disk, memory), it causes the worker to prematurely truncate multi-turn investigations.
- **Evidence:** The prompt explicitly uses an "ANTI-EXAMPLE" that discourages thoroughness. For conditional tasks ("check if X, if not Y"), the worker correctly tries to condense into a shell `&&` chain, but if that chain fails or is too complex, the worker refuses to take a second turn.

### 2. Supervisor Blindness in Evals
In production, Zerg uses **Durable Runs** where workers complete in the background and trigger continuations.
- **Evaluation Issue:** To avoid deadlocks in the test runner (which disables background processing), `spawn_worker` was recently changed to default to `fire-and-forget`.
- **Outcome:** The supervisor receives a "Job queued" confirmation, says "I've queued a worker," and completes the run. The `EvalRunner` then drains the worker synchronously *after* the supervisor has already finished.
- **Result:** The supervisor NEVER sees the findings in evaluation mode, leading to 0% scores on tasks that require reporting a result.

### 3. Technical Debt (Bugs Found)
During reproduction, three significant bugs were uncovered:
- **Datetime Serialization:** `_call_tool_sync` crashes when a tool returns a `datetime` object (e.g., `runner_list`) because `json.dumps` doesn't handle it.
- **Datetime Comparison:** `get_worker_metadata_async` crashes when comparing offset-naive vs offset-aware datetimes.
- **EvalRunner Persistence:** Workers processed in `EvalRunner` often remain in "running" status due to session handling/flushing issues in the synchronous drain loop.

## Proposed Plan

### Phase 1: Robustness Fixes (Infrastructure)
- **Fix JSON Serialization:** Update `zerg_react_agent.py` to use a custom JSON encoder that handles `datetime` and `date`.
- **Fix Datetime Mismatch:** Update `supervisor_tools.py` to ensure all comparisons use timezone-aware UTC.
- **Fix EvalRunner Status:** Ensure `db.commit()` and `db.refresh()` are used correctly in `_process_queued_worker_jobs`.

### Phase 2: Prompt Evolution
- **Relax Worker Prompt:** Soften the "ONE command" rule to "Minimal turns" and add a "Conditional Execution" section to `BASE_WORKER_PROMPT`.
- **Supervisor "Wait" Mode for Evals:** Implement an `[eval:wait]` task marker. When present, `EvalRunner` will allow `spawn_worker(wait=True)`. This will trigger the "Roundabout" monitor which we will update to drain the worker synchronously ONLY in eval mode.

### Phase 3: Evaluation Quality
- **Update Live Eval Dataset:** Update the `delegation_task_clarity` task to include the `[eval:wait]` marker.
- **Evidence Verification:** Verify that the supervisor correctly synthesizes the worker result into the final response using the Evidence Mounting system.

## Next Steps
1. Implement Phase 1 robustness fixes.
2. Implement Phase 2 prompt and tool changes.
3. Verify with `scripts/reproduce_delegation_failure.py`.
