# Mount → Reason → Prune (Implementation Spec)

## Goal

Make the supervisor reliably answer using worker evidence **without**:
- writing parsers for tool output
- enforcing schemas on worker output
- polluting the supervisor’s long-lived thread with raw logs/tool dumps

This spec implements “agent orchestration as paged memory”:
- workers generate evidence (big, messy)
- supervisor reasons with mounted evidence (ephemeral)
- persistent memory stores only the final decision

---

## Problem statement (current failure mode)

Today, a worker can:
- successfully execute tools (e.g. `ssh_exec`)
- yet produce an empty or misleading natural-language “final message”

When that happens, the supervisor often only sees a thin “result.txt” blob (or a confusing worker summary) and can conclude “couldn’t check”, even though the raw tool output contains the answer.

Root cause: **supervisor decision context is not guaranteed to include the ground-truth execution evidence.**

---

## Design overview

We introduce an always-available, deterministic evidence flow that does not rely on model formatting:

1) **Workers write a canonical trace** (already true via artifacts + tool event stream).
2) **Supervisor gets an ephemeral “Evidence Mount”** during inference whenever worker evidence exists for the current run.
3) **Supervisor commits only its final answer** to the long-lived thread (prune the mounted evidence).

We do not parse tool output. The mount is “dumb packaging”: tail/head + pointers + strict budgets.

### Important: no discontinuities / no “empty output” special-cases

We explicitly avoid branching the pipeline based on whether a worker’s final prose is empty.

Instead:
- If worker evidence exists for the current `run_id`, we mount it for the supervisor *every time* we call the supervisor model for that run (within budgets).
- The supervisor can answer from evidence regardless of whether the worker wrote a “good summary”.

---

## Invariants (system-enforced)

### Evidence preservation
- Every worker tool call output is persisted to the artifact store.

### Evidence availability
- During a supervisor run, the system can mount evidence for workers spawned by that run.

### Non-pollution
- Mounted evidence never becomes a persistent `thread_messages` row in the supervisor thread.
- The supervisor thread stores only user messages + supervisor answers (+ minimal provenance pointers).

### Budgets
- Evidence mounts are capped by strict limits (bytes/lines/tokens) to control latency and avoid context blowups.

---

## Non-goals

- No required output schema from workers.
- No “structured facts/evidence JSON.”
- No domain-specific parsing (df/docker/log regex).
- No extra LLM “summarization layer” by default.

---

## Terminology

- **Supervisor run:** a single request/response cycle identified by `run_id`.
- **Worker job:** a spawned unit of work correlated to a supervisor run via `supervisor_run_id`.
- **Trace:** canonical worker artifacts (`thread.jsonl`, `tool_calls/*.txt`, `metrics.jsonl`, etc.).
- **Evidence mount:** ephemeral injection of trace excerpts into supervisor prompt messages.

---

## Data and correlation requirements

We already have the needed correlation signals:
- `AgentRun.id` as `run_id`
- `WorkerJob.supervisor_run_id` (set by `spawn_worker` using supervisor context)
- `WorkerJob.worker_id` → artifact directory

We will treat `WorkerJob.supervisor_run_id == run_id` as the primary lookup.

---

## Evidence Mount: selection and rendering (no parsing)

### Inputs
- `run_id`
- `owner_id`
- `budget` (max bytes or token estimate)

### Selection policy (intentionally dumb)
For each worker in this run:
- include worker task (from DB)
- include the last N bytes of:
  - `result.txt` (if exists)
  - the most recent `tool_calls/*.txt` files (in reverse chronological order) (prefer these over worker prose)
  - optionally: the tail of `thread.jsonl`
- include pointers to full artifacts:
  - worker_id
  - file names available in the worker directory

No parsing. No interpretation. Just packaging.

### Rendering
Inject a single ephemeral `SystemMessage` (or equivalent) like:

```
[INTERNAL EVIDENCE - EPHEMERAL]
Run: <run_id>
Workers: <count>

Worker <job_id> (<worker_id>)
Task: ...

--- tool_calls/002_ssh_exec.txt (tail 8KB) ---
...

Pointers:
- read_worker_result(<job_id>)
- read_worker_file(<job_id>, "tool_calls/002_ssh_exec.txt")
```

This is a “mount”: a context layer visible to the supervisor model for this inference call only.

---

## Where the mount happens (architecture)

### Preferred: supervisor AgentRunner pre-inference injection

Inject evidence mounts into the supervisor’s working context the same way we already inject connector status:
- Do not persist to DB
- Recompute each inference call during the run

Practical shape:

- Extend the supervisor execution pipeline to call a context assembler:
  - base messages: persistent thread messages (excluding any stale system messages)
  - ephemeral messages:
    - system prompt + connector context (already exists)
    - evidence mount (new)

This keeps “Mount” as a deterministic system step and does not require the supervisor to remember to call `read_worker_file`.

### Alternative: tool-level mount via ephemeral tool messages (optional)

If the supervisor explicitly requests evidence (“show me worker outputs”), we can return it through a tool call, but mark that tool output as ephemeral (not saved to the thread). This is optional and not required for MVP.

---

## Prune strategy

Pruning is achieved by construction:
- the evidence mount is never saved to `thread_messages`
- only the final supervisor assistant message is persisted

If we introduce any new ephemeral tool responses, they must be excluded from DB persistence similarly.

---

## Supervisor behavior changes (no new LLM calls)

This spec does not add another model layer. It changes what the supervisor sees during its existing run.

It also implies a behavioral expectation:
- If workers are spawned, the supervisor should continue reasoning until it can answer using evidence.

We should not enforce this via rigid heuristics. We should:
- make evidence visible as soon as it exists
- keep “pending workers” visible via events/UI
- allow the supervisor to decide when it has enough

---

## Work plan (concrete changes)

### 1) Evidence mount builder
Add a small module that:
- lists worker jobs for `(owner_id, run_id)`
- reads a bounded subset of their artifact files
- returns a single rendered text block

Must support:
- hard budgets (bytes/lines)
- safe truncation with pointers
- owner_id scoping (no cross-user leakage)

### 2) Inject mount into supervisor working context
Modify the supervisor inference pipeline to include the evidence mount as an ephemeral system message.

Candidate integration points:
- `zerg.managers.agent_runner.AgentRunner.run_thread()` (supervisor-only path) or
- `zerg.services.supervisor_service.SupervisorService.run_supervisor()` before invoking the agent runnable

Important: preserve the existing “system prompt is injected at runtime” approach.

### 3) SSE/UI impact (no required changes for MVP)
The UI already shows worker tool progress via SSE. Mounting is internal and should not affect the frontend.

Optional:
- add a debug toggle to show “evidence mount used” markers for dev.

### 4) Provenance pointers
Persist a small pointer in the supervisor’s final answer (or metadata) such as:
- “Evidence: worker job(s) 1, 2 (run_id=48)”

This is for humans/debugging, not as a schema contract.

---

## Testing strategy

We can test without parsing:
- Unit test: mount builder respects budgets and includes pointers.
- Integration test: simulate a worker with large `tool_calls/*.txt` and ensure the supervisor receives an ephemeral evidence message (without DB persistence).
- Regression: reproduce “worker final message empty” and confirm supervisor can still answer when mount includes tool output.

---

## Operational considerations

### Budgets and latency
- Keep mounts small by default (e.g., 8–32KB total).
- Prefer tail of tool outputs.
- Include pointers for deep drill-down.

### Retention / GC
- Worker artifacts can be GC’d by retention policy.
- If a supervisor answer references a worker_id, consider pinning or extending retention for that worker.

### Security
- Evidence mounting must be scoped by `owner_id`.
- Filter by `run_id` to avoid leaking other runs into the current context.

---

## Rollout

1) Implement mount builder + injection for supervisor runs only.
2) Verify that infrastructure tasks produce correct supervisor answers even when worker prose is empty.
3) Iterate on mount selection policy + budgets.
4) Optional: extend mounting to supervisor “recent worker activity” and/or tool-level ephemeral responses.
