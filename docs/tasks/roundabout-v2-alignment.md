# Roundabout v2.0 Alignment - Task Document

**Created:** 2025-12-08
**Implemented:** 2025-12-08
**Status:** ✅ Improvements 1-3 Complete, Improvement 4 Deferred
**Priority:** Medium (polish, not blocking)

---

## Executive Summary

The roundabout monitoring system has been updated to use LLM mode by default (v2.0 philosophy), but there are vestigial heuristics embedded in prompts and data structures that contradict the "trust the AI" approach. This document catalogs findings and proposes improvements.

---

## Research Findings

### Current Flow Analysis

```
User Question
    ↓
Jarvis → Supervisor (decides: direct vs delegate)
    ↓
spawn_worker() → Worker executes with ssh_exec, etc.
    ↓
RoundaboutMonitor polls every 5s
    ↓
LLM Decider interprets status → wait/exit/cancel/peek
    ↓
Worker completes → Supervisor synthesizes result
    ↓
Response to user
```

### What's Working Well

| Component                   | Status | Notes                                     |
| --------------------------- | ------ | ----------------------------------------- |
| Supervisor delegation logic | ✅     | Prompt is explicit about SSH limitation   |
| Worker tool selection       | ✅     | Focused toolset, clear tasks              |
| Event-driven UI progress    | ✅     | Tool events work without complexity       |
| Conservative defaults       | ✅     | Decider defaults to "wait" on uncertainty |
| Fast path on completion     | ✅     | Short-circuits when worker finishes       |
| Result synthesis            | ✅     | Supervisor translates technical → human   |

### Issues Found

Four vestigial heuristics that contradict v2.0 philosophy:

1. **Keyword-based completion detection in prompt**
2. **Pre-computed `is_stuck` judgment**
3. **No automatic history injection**
4. **Cancel action doesn't actually cancel**

---

## Files & Functions Involved

### Primary Files

| File                                  | Purpose                   | LOC |
| ------------------------------------- | ------------------------- | --- |
| `zerg/services/llm_decider.py`        | LLM decision logic        | 324 |
| `zerg/services/roundabout_monitor.py` | Polling loop & monitoring | 914 |
| `zerg/prompts/supervisor_prompt.py`   | Supervisor system prompt  | 186 |

### Key Functions

```
llm_decider.py:
├── build_llm_prompt()          # Builds decider prompt (has keyword hints)
├── build_decision_payload()    # Builds payload (has is_stuck field)
├── call_llm_decider()          # Calls gpt-4o-mini for decision
└── decide_roundabout_action()  # Main entry point

roundabout_monitor.py:
├── DecisionContext             # Dataclass with is_stuck field
├── make_heuristic_decision()   # DEPRECATED - v1.0 heuristics
└── RoundaboutMonitor.wait_for_completion()  # Main polling loop

supervisor_prompt.py:
└── SUPERVISOR_SYSTEM_PROMPT    # Supervisor behavior instructions
```

### Supporting Files

| File                                     | Relevance                        |
| ---------------------------------------- | -------------------------------- |
| `zerg/services/supervisor_service.py`    | Spawns workers, receives results |
| `zerg/tools/builtin/supervisor_tools.py` | spawn_worker, list_workers tools |
| `zerg/services/worker_artifact_store.py` | Worker result persistence        |

---

## Improvement 1: Remove Keyword Hints from Decider Prompt

### Problem

Current prompt embeds keyword-matching heuristics:

```python
# llm_decider.py:193-201
system_prompt = """...
- exit: Return immediately if the task has clearly produced a final answer
  or result. Look for output containing "Result:", "Summary:", "Done.",
  "Completed", or similar completion indicators.
..."""
```

This is pattern-matching disguised as LLM decision. If worker output says "Backup ran successfully at 3am" (no magic words), LLM might not recognize completion.

### Proposed Fix

Trust semantic understanding, not keywords:

```python
system_prompt = """...
- exit: Return immediately if the worker appears to have completed its task
  and produced useful output. Use your judgment to recognize completion
  regardless of specific wording.
..."""
```

### Files to Modify

- `zerg/services/llm_decider.py` - `build_llm_prompt()` function (lines 193-203)

### Effort

~15 minutes

---

## Improvement 2: Remove Pre-Computed `is_stuck` Judgment

### Problem

System computes `is_stuck` boolean before LLM sees data:

```python
# roundabout_monitor.py - DecisionContext
@dataclass
class DecisionContext:
    is_stuck: bool  # Pre-computed judgment!
    stuck_operation_seconds: float
    ...

# Computed here:
is_stuck = stuck_operation_seconds > ROUNDABOUT_STUCK_THRESHOLD  # 30s
```

The LLM receives `is_stuck: True/False` rather than raw data. This contradicts v2.0: "let LLM interpret status."

45 seconds might be fine for `du -sh /var` (large directory), but stuck for `ls` (instant command). The system can't know this; the LLM can.

### Proposed Fix

Option A: Remove `is_stuck`, keep raw timing:

```python
# Payload includes:
{
    "current_op_elapsed_ms": 45000,
    "current_op_name": "ssh_exec",
    "current_op_args": "du -sh /var/*"
}
# LLM decides if 45s is reasonable for du -sh
```

Option B: Keep `is_stuck` as hint but add context:

```python
{
    "is_stuck": True,
    "is_stuck_note": "Operation exceeded 30s threshold, but this is just a hint - use your judgment based on the command"
}
```

### Files to Modify

- `zerg/services/roundabout_monitor.py` - `DecisionContext` dataclass, `_build_context()` method
- `zerg/services/llm_decider.py` - `build_decision_payload()`, `LLMDecisionPayload` dataclass

### Effort

~1 hour

---

## Improvement 3: Auto-Inject Recent Worker History

### Problem

Supervisor prompt says "check list_workers first" but LLMs often skip this step:

```
User (t=0): "Check my backups"
→ Worker runs, reports healthy

User (t=2min): "Are my backups working?"
→ Supervisor spawns ANOTHER worker instead of referencing recent result
```

This wastes resources and time.

### Proposed Fix

Automatically inject recent worker history into supervisor context:

```python
# In supervisor_service.py, before LLM call:
recent_workers = list_workers(owner_id, limit=5, since_minutes=10)

if recent_workers:
    context_addition = """
## Recent Worker Activity (last 10 minutes)
You already have results from these workers - check if they answer the user's question before spawning new ones:

"""
    for w in recent_workers:
        context_addition += f"- {w.worker_id}: '{w.task}' - {w.status} ({w.elapsed})\n"

    # Inject into messages before user's question
```

### Files to Modify

- `zerg/services/supervisor_service.py` - Add history injection before LLM call
- Possibly `zerg/prompts/supervisor_prompt.py` - Add instruction to use injected history

### Effort

~2 hours

---

## Improvement 4: Implement Actual Worker Cancellation

### Problem

When decider returns "cancel", the system doesn't actually kill the worker:

```python
# Current behavior:
if decision == "cancel":
    # Just stops monitoring and returns
    return RoundaboutResult(status="cancelled", ...)
    # Worker process continues running!
```

The "cancel" action is a lie - it cancels _monitoring_, not the worker.

### Proposed Fix

Implement actual cancellation:

```python
# roundabout_monitor.py
if decision == "cancel":
    # Actually cancel the worker
    await self._cancel_worker(job_id)
    return RoundaboutResult(status="cancelled", ...)

async def _cancel_worker(self, job_id: int):
    """Cancel a running worker."""
    # Option 1: Set flag that worker checks
    await db.execute(
        update(WorkerJob).where(WorkerJob.id == job_id)
        .values(cancellation_requested=True)
    )

    # Option 2: If worker is a subprocess, send SIGTERM
    # (depends on execution model)
```

Worker needs to check cancellation flag periodically:

```python
# In worker execution loop:
if await check_cancellation_requested(job_id):
    raise WorkerCancelledException("Cancelled by supervisor")
```

### Files to Modify

- `zerg/services/roundabout_monitor.py` - Add `_cancel_worker()` method
- `zerg/services/zerg_react_agent.py` - Add cancellation check in tool loop
- `zerg/models/worker_job.py` - Add `cancellation_requested` field (migration needed)

### Effort

~4 hours (includes migration)

### Note

This is lower priority. Current behavior (stop monitoring) is acceptable for most cases. True cancellation is a nice-to-have for long-running workers that go off track.

---

## Implementation Priority

| #   | Improvement                  | Effort  | Impact | Priority |
| --- | ---------------------------- | ------- | ------ | -------- |
| 1   | Remove keyword hints         | 15 min  | Medium | High     |
| 2   | Remove is_stuck pre-judgment | 1 hour  | Medium | High     |
| 3   | Auto-inject worker history   | 2 hours | High   | Medium   |
| 4   | Actual worker cancellation   | 4 hours | Low    | Low      |

**Recommended order:** 1 → 2 → 3 → 4 (if time permits)

**Total effort:** ~7 hours for all four improvements

---

## Success Criteria

After implementation:

1. **No keyword dependencies** - Decider recognizes completion semantically
2. **No pre-computed judgments** - LLM receives raw timing, decides "stuck"
3. **Reduced redundant workers** - Supervisor references recent work before spawning
4. **True cancellation** - "cancel" action actually stops worker execution

### Test Cases

```python
# Test 1: Completion without magic words
worker_output = "Backup ran successfully at 3am. All files synced."
# Should recognize as complete even without "Result:" or "Done."

# Test 2: Stuck judgment varies by command
payload_1 = {"command": "ls", "elapsed_ms": 10000}  # 10s for ls = stuck
payload_2 = {"command": "du -sh /var", "elapsed_ms": 45000}  # 45s for du = normal
# LLM should judge differently

# Test 3: Recent worker reference
# Ask same question twice in 5 minutes
# Second time should reference first result, not spawn new worker

# Test 4: Cancellation works
# Spawn worker, request cancel, verify worker actually stops
```

---

## Related Documents

- [super-siri-architecture.md](../specs/super-siri-architecture.md) - Master v2.0 spec
- [worker-supervision-roundabout.md](../specs/worker-supervision-roundabout.md) - Monitoring spec
- [ops_control_plane.md](../ops_control_plane.md) - SSH security model

---

_End of Task Document_
