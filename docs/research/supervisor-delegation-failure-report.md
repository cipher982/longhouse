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

## Implementation Progress

### Phase 1: Robustness Fixes (Infrastructure) ✅ COMPLETE
- **JSON Serialization:** Fixed in `zerg_react_agent.py` using custom handler for `datetime`.
- **Datetime Mismatch:** Fixed in `supervisor_tools.py` using `utc_now_naive()`.
- **EvalRunner Persistence:** Fixed in `runner.py` with session refreshes and `utc_now_naive()`.

### Phase 2: Prompt Evolution ✅ COMPLETE
- **Relax Worker Prompt:** Softened "ONE command" rule to "Goal-Oriented Execution" in `templates.py`.
- **Supervisor "Wait" Mode for Evals:** Implemented `[eval:wait]` logic in `supervisor_tools.py` and `RoundaboutMonitor.py`.

### Phase 3: Evaluation Quality (In Progress)
- **Update Live Eval Dataset:** Updated `delegation_task_clarity` and added `delegation_multi_step` with `[eval:wait]`.
- **Evidence Verification:** Pending live test run.

## Next Steps
1. Run live evaluations to verify the fix.
2. Confirm supervisor synthesis of worker findings.
