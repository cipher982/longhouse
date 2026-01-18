# Mount → Reason → Prune (Implementation Spec)

> **Status**: Partially superseded
> **Updated**: 2026-01-18
>
> This spec proposed auto-mounting evidence at the LLM-call boundary. The actual implementation
> (2026-01-18) uses **on-demand tool calls** instead:
> - `get_worker_evidence(job_id, budget_bytes)` — fetch worker artifacts when needed
> - `get_tool_output(artifact_id)` — fetch large tool outputs stored by reference
>
> This "pull" model aligns better with the Claude Code pattern of progressive disclosure
> and avoids unconditionally inflating context. The core principles (pointers over raw data,
> deterministic compilation, byte budgets) remain valid.
>
> See `docs/agent-improvements-progress.md` for implementation status.

## Goal

Make the supervisor reliably answer using worker evidence **without**:
- writing parsers for tool output
- enforcing schemas on worker output
- polluting the supervisor's long-lived thread with raw logs/tool dumps
- requiring extra "dig in" tool calls to access evidence

This spec implements "agent orchestration as paged memory":
- workers generate evidence (big, messy)
- supervisor reasons with mounted evidence (ephemeral, expanded at LLM-call boundary)
- persistent memory stores only pointers + final decisions

---

## Problem statement (current failure mode)

Today, a worker can:
- successfully execute tools (e.g. `ssh_exec`)
- yet produce an empty or misleading natural-language "final message"

When that happens, the supervisor often only sees a thin "result.txt" blob (or a confusing worker summary) and can conclude "couldn't check", even though the raw tool output contains the answer.

Root cause: **supervisor decision context is not guaranteed to include the ground-truth execution evidence.**

---

## Design overview

We introduce an always-available, deterministic evidence flow that does not rely on model formatting:

1. **Workers write a canonical trace** (already true via artifacts + tool event stream).
2. **spawn_worker returns a compact payload** with pointers + tool index (persisted to thread).
3. **LLM wrapper expands evidence markers** before each LLM call within the ReAct loop (ephemeral).
4. **Only the compact payload persists** — raw evidence is never saved to thread_messages.

### Key insight: mount at the LLM-call boundary

The ReAct loop involves multiple LLM calls:
```
LLM call #1 → decides to spawn_worker
Tool executes → spawn_worker returns compact payload
LLM call #2 → reasons about worker result  ← EVIDENCE MOUNTED HERE
Maybe more tools...
LLM call #N → final response
```

Evidence must be mounted **before each LLM call**, not just once at run start. This requires intercepting the LLM itself, not the message loading at run start.

---

## Invariants (system-enforced)

### Evidence preservation
- Every worker tool call output is persisted to the artifact store (`tool_calls/*.txt`).

### Evidence availability
- During a supervisor run, the LLM wrapper can expand evidence for any worker spawned by that run.

### Non-pollution
- Mounted evidence never becomes a persistent `thread_messages` row.
- The supervisor thread stores only: user messages + compact tool responses + supervisor answers.

### Budgets
- Evidence mounts are capped by strict limits (bytes/tokens) to control latency and context size.

---

## Architecture

### Layer 1: Artifact Store (already exists)

Workers persist to disk:
```
/data/swarmlet/workers/{worker_id}/
├── metadata.json        # Status, timestamps, task
├── result.txt           # Worker's final AI message (may be empty/garbage)
├── thread.jsonl         # Full conversation
└── tool_calls/
    ├── 001_ssh_exec.txt      # Raw tool output
    ├── 002_http_request.txt
    └── ...
```

### Layer 2: spawn_worker return (compact, persisted)

When `spawn_worker(wait=True)` completes, it returns a structured payload:

```
Worker job 123 completed successfully.
Duration: 45.2s | Worker ID: abc-123

Tool Index:
  1. ssh_exec [exit=0, 234ms, 1847 bytes]
  2. ssh_exec [exit=1, 156ms, 523 bytes]  ← FAILED

Summary: Checked disk space and container status on clifford.

[EVIDENCE:run_id=48,job_id=123,worker_id=abc-123]
```

Key elements:
- **Tool index**: Execution metadata (not domain parsing) — which tools ran, exit codes, durations, output sizes
- **Summary**: Worker's prose (may be garbage, that's ok)
- **Evidence marker**: Pointer for the LLM wrapper to expand

This payload becomes a `ToolMessage` and is persisted to thread_messages. It's small (~500 bytes).

### Layer 3: Evidence Compiler

A deterministic module that assembles evidence within budget:

```python
class EvidenceCompiler:
    def compile(
        self,
        run_id: int,
        owner_id: int,
        budget_bytes: int = 32000,
    ) -> dict[int, str]:
        """
        Returns {job_id: expanded_evidence} for all workers in this run.

        Prioritization:
        1. Failed tool outputs (exit_code != 0)
        2. Most recent tool outputs
        3. Earlier outputs (if budget remains)

        Truncation:
        - Head (first 1KB) + tail (last N KB) per artifact
        - Include byte offsets so LLM knows what was truncated
        """
```

### Layer 4: LLM Wrapper (ephemeral expansion)

Wraps the base LLM to expand evidence markers before each call:

```python
class EvidenceMountingLLM:
    """Wraps base LLM to mount evidence before each API call."""

    def __init__(self, base_llm, run_id: int, owner_id: int):
        self.base_llm = base_llm
        self.run_id = run_id
        self.owner_id = owner_id
        self.compiler = EvidenceCompiler()

    async def ainvoke(self, messages, **kwargs):
        # Expand evidence markers in ToolMessages
        augmented = self._mount_evidence(messages)
        return await self.base_llm.ainvoke(augmented, **kwargs)

    def _mount_evidence(self, messages):
        # Detect [EVIDENCE:...] markers in ToolMessages
        # Expand with compiled evidence from artifact store
        # Return augmented messages (never persisted)
```

This wrapper:
- Intercepts at the right point (before each LLM API call)
- Operates outside LangGraph's state management (no persistence side effects)
- Is transparent to the rest of the system

### LangGraph State Isolation

Critical: LangGraph checkpoints and persists message state. Our approach is safe because:

```
LangGraph state (persisted):
  └── ToolMessage: "Worker 123 completed. [EVIDENCE:job_id=123]"

LLM wrapper (ephemeral, outside state):
  └── Expands marker → full tool outputs
  └── Sends to API
  └── Returns response
  └── LangGraph never sees expanded content
```

The expansion happens at the API boundary, not in the state machine.

---

## Evidence marker format

Markers are embedded in the ToolMessage content:

```
[EVIDENCE:run_id=48,job_id=123,worker_id=abc-123]
```

The LLM wrapper parses these and expands them with compiled evidence.

### Expanded form (what the LLM sees)

```
[EVIDENCE:run_id=48,job_id=123,worker_id=abc-123]

--- Evidence for Worker 123 (abc-123) ---
Budget: 16KB | Priority: failures first

[FAILED] tool_calls/002_ssh_exec.txt (523 bytes, exit=1):
Error: Connection refused
ssh: connect to host clifford port 22: Connection refused

tool_calls/001_ssh_exec.txt (1847 bytes, exit=0, showing tail 1500B):
...
Filesystem      Size  Used Avail Use% Mounted on
/dev/sda1       100G   45G   55G  45% /
/dev/sdb1       500G  200G  300G  40% /data
tmpfs           7.8G     0  7.8G   0% /dev/shm

--- End Evidence ---
```

---

## Evidence Compiler: selection and prioritization

### Inputs
- `run_id`
- `owner_id`
- `budget_bytes` (default: 32KB total across all workers)

### Priority order
1. **Failed tools first**: exit_code != 0, stderr content
2. **Most recent tools**: later sequence numbers
3. **Larger outputs**: more likely to contain useful detail

### Truncation strategy
For each artifact that exceeds per-file budget:
- Include **head** (first 1KB) — shows command/context
- Include **tail** (remaining budget) — shows final output/errors
- Include byte offset markers: `[...truncated 45KB...]`

### Budget allocation
- If single worker: full budget
- If multiple workers: divide budget, prioritize failures

---

## spawn_worker changes

### Current return (result.txt only)
```python
def format_roundabout_result(result: RoundaboutResult) -> str:
    # Returns result.txt content, truncated
```

### New return (compact payload + marker)
```python
def format_roundabout_result(result: RoundaboutResult) -> str:
    lines = []
    lines.append(f"Worker job {result.job_id} completed ({result.status}).")
    lines.append(f"Duration: {result.duration_seconds:.1f}s | Worker ID: {result.worker_id}")
    lines.append("")

    # Tool index (execution metadata, not domain parsing)
    if result.tool_index:
        lines.append("Tool Index:")
        for t in result.tool_index:
            status = "FAILED" if t.exit_code != 0 else "ok"
            lines.append(f"  {t.seq}. {t.name} [{status}, {t.duration_ms}ms, {t.output_bytes}B]")
        lines.append("")

    # Summary (worker's prose, may be garbage)
    if result.summary:
        lines.append(f"Summary: {result.summary[:500]}")
        lines.append("")

    # Evidence marker for LLM wrapper expansion
    lines.append(f"[EVIDENCE:run_id={result.run_id},job_id={result.job_id},worker_id={result.worker_id}]")

    return "\n".join(lines)
```

---

## Integration points

### 1. EvidenceCompiler module
New file: `zerg/services/evidence_compiler.py`

### 2. LLM wrapper
Modify `zerg/agents_def/zerg_react_agent.py`:
- Wrap the LLM with `EvidenceMountingLLM` for supervisor runs
- Pass run_id and owner_id through context

### 3. spawn_worker return
Modify `zerg/services/roundabout_monitor.py`:
- Build tool index from worker artifacts
- Include evidence marker in return

### 4. Tool index collection
Modify `zerg/services/worker_runner.py`:
- Track exit codes and output sizes during execution
- Store in worker metadata for retrieval

---

## Non-goals

- No required output schema from workers.
- No domain-specific parsing (df/docker/log regex).
- No extra LLM "summarization layer".
- No changes to frontend/SSE (mounting is internal).

---

## Testing strategy

### Unit tests
- Evidence compiler respects budgets
- Prioritization works (failures first)
- Truncation produces head+tail
- Marker parsing is robust

### Integration tests
- Worker with large tool outputs → supervisor sees evidence without DB persistence
- Worker with empty result.txt → supervisor still answers correctly
- Multiple workers → budget divided appropriately

### Regression tests
- Reproduce "worker final message empty" → confirm supervisor uses mounted evidence
- Verify thread_messages only contains compact payloads, not raw evidence

---

## Operational considerations

### Budgets and latency
- Default: 32KB total evidence budget
- Per-worker: 16KB if single, divided if multiple
- Truncation adds ~100ms latency (disk reads)

### Retention / GC
- Worker artifacts can be GC'd by retention policy
- Evidence markers become stale after GC (graceful degradation: show "evidence no longer available")

### Security
- Evidence compiler scoped by `owner_id`
- Filtered by `run_id` to prevent cross-run leakage
- LLM wrapper must validate ownership before expansion

---

## Rollout

1. Implement EvidenceCompiler + unit tests
2. Implement LLM wrapper + integration with zerg_react_agent
3. Update spawn_worker return format (tool index + marker)
4. Verify supervisor answers correctly with empty worker prose
5. Tune budget/prioritization based on real usage
