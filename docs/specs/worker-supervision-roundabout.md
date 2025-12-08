# Worker Supervision & Monitoring

**Version:** 2.1
**Date:** December 2025
**Status:** Simplified Reference
**Parent Spec:** [super-siri-architecture.md](./super-siri-architecture.md)

---

## Overview

This document covers worker monitoring patterns and event schemas. For the full philosophy ("Trust the AI", guardrails vs heuristics, Paid Intern Test), see [super-siri-architecture.md](./super-siri-architecture.md).

**Key insight:** The problem wasn't polling - it's natural for a supervisor to check on workers (like glancing at a second monitor). The problem was the `make_heuristic_decision()` function that pre-programmed decisions. Remove the decision engine, keep monitoring.

---

## The "Second Monitor" Pattern

Supervisor checking on workers is natural and useful:

```python
# Supervisor can check status whenever it wants
status = get_worker_status(job_id=123)

# Supervisor (LLM) interprets and decides:
# "Worker has been running 45s on du -sh. That's normal. I'll wait."
# OR: "Worker has been stuck for 3 minutes. I'll cancel."
```

**What was wrong (v1.0):**

```python
def make_heuristic_decision(context):  # ❌ DELETE THIS
    if context.stuck_seconds > 60:
        return "CANCEL"  # Pre-programs LLM decisions
```

**What's right (v2.0):**

The LLM reads status and decides. No hardcoded rules.

---

## Event Schema

### Worker Lifecycle Events

```json
// Worker started
{"type": "worker_started", "job_id": 123, "worker_id": "2025-12-07T20-33-15_...", "task": "Check disk", "timestamp": "..."}

// Worker completed
{"type": "worker_complete", "job_id": 123, "status": "success", "duration_ms": 16547, "timestamp": "..."}

// Summary ready (for list_workers)
{"type": "worker_summary_ready", "job_id": 123, "summary": "Cube disk at 78%, healthy"}
```

### Tool Activity Events (UI Progress)

```json
// Tool started
{"type": "worker_tool_started", "worker_id": "...", "tool_name": "ssh_exec", "tool_call_id": "call_abc", "args_preview": "cube: df -h"}

// Tool completed
{"type": "worker_tool_completed", "worker_id": "...", "tool_name": "ssh_exec", "tool_call_id": "call_abc", "duration_ms": 1800, "result_preview": "78% used"}

// Tool failed
{"type": "worker_tool_failed", "worker_id": "...", "tool_name": "ssh_exec", "error": "Connection refused"}
```

### Supervisor Events

```json
{"type": "supervisor_started", "run_id": 456, "task": "Check my servers"}
{"type": "supervisor_complete", "run_id": 456, "result": "Your servers are healthy..."}
{"type": "supervisor_error", "run_id": 456, "error": "Worker timeout"}
```

---

## Worker Status Tool

```python
async def get_worker_status(job_id: int) -> dict:
    """Query current worker state on-demand."""
    return {
        "status": "running",  # queued, running, success, failed
        "elapsed_ms": 45000,
        "current_operation": {
            "tool": "ssh_exec",
            "args": {"host": "cube", "command": "du -sh /var/*"},
            "elapsed_ms": 35000
        },
        "completed_operations": [
            {"tool": "ssh_exec", "command": "df -h", "duration_ms": 1800}
        ]
    }
```

Supervisor (LLM) interprets this and decides what to do. No heuristics.

---

## Worker Artifacts

```
/data/swarmlet/workers/{worker_id}/
├── metadata.json     # Status, timestamps, model
├── thread.jsonl      # Full LLM conversation trace
├── result.txt        # Final natural language result
└── tool_calls/
    ├── 001_ssh_exec.txt
    └── 002_ssh_exec.txt
```

---

## Migration: Code to Remove

```python
# DELETE: RoundaboutMonitor class
class RoundaboutMonitor:
    async def wait_for_completion(self, job_id):
        while True:  # ❌ Polling loop
            await asyncio.sleep(5)
            decision = make_decision()  # ❌ Heuristics
            ...

# DELETE: Heuristic decision engine
def make_heuristic_decision(context):  # ❌
    if context.stuck > 60: return "CANCEL"
    ...

# DELETE: RoundaboutDecision enum, DecisionContext dataclass
```

## Migration: Code to Keep

```python
# KEEP: Tool activity events
await event_bus.publish(EventType.WORKER_TOOL_STARTED, {...})
await event_bus.publish(EventType.WORKER_TOOL_COMPLETED, {...})

# KEEP: SSE forwarding to UI
event_bus.subscribe(EventType.WORKER_TOOL_STARTED, forward_to_sse)
```

---

## Autonomous Decision Examples

### Example 1: Normal Progress

```
Worker state: ssh_exec running for 35s on du -sh /var/*
Supervisor: "du -sh takes 30-60s on large dirs. Normal. I'll wait."
```

### Example 2: Early Exit

```
Worker state: df -h completed (78% used), du -sh still running
Supervisor: "I have enough info from df -h. I can answer now."
```

### Example 3: Intervention

```
Worker state: HTTP request running for 90s
Supervisor: "Health checks return in 1-5s. This is stuck. Cancel."
```

**Key:** Supervisor makes these decisions by reasoning, not by `if elapsed > 60`.

---

_For full philosophy and architecture, see [super-siri-architecture.md](./super-siri-architecture.md)._
