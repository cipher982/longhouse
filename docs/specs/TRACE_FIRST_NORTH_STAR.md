# Trace-First, LLM-King Orchestration (North Star)

## Why this exists

Zerg/Swarmlet is an AI agent orchestration platform. The long-term goal is a single “supervisor” agent that can reason and act directly with deep, continuous context (“no workers”). Today, models and engineering constraints force us to control context size and latency. The worker pattern exists purely as a scalability hack: isolate messy exploration (terminals/logs) in short-lived contexts and feed the supervisor only what it needs to answer.

This document is the north star: what we’re building *toward*, and the rules we won’t violate while getting there.

---

## Core principles

### 1) The LLM is king

- We do **not** constrain the model with rigid schemas, required JSON, or brittle output contracts.
- We do **not** write domain-specific parsers (no “df -h” regex pipelines). The model should interpret raw outputs.
- We instead constrain **systems**: what gets persisted, what evidence is always available, and how memory is bounded.

### 2) Trace is truth (append-only)

Every run (supervisor + workers) produces an append-only, replayable execution trace:
- tool calls + raw outputs
- intermediate messages
- timings, failures, retries
- correlation IDs (run_id, worker_id, tool_call_id)

The trace is the canonical truth. Human-readable answers are derived views.

### 3) Separate stores: trace vs conversation vs working context

We maintain three distinct “memory surfaces”:

- **Trace store (canonical, big, messy):** full evidence, replayable.
- **Conversation store (persistent, small):** what we decided / told the user.
- **Working context (ephemeral):** a transient view assembled for *this* inference call.

This prevents the failure mode: tools succeeded, but the conversation artifact implies failure.

### 4) Context is paged memory, not agent note-passing

Workers are not “collaborators passing notes.” They are isolated contexts for generating evidence. The system’s job is to:
- preserve evidence reliably (trace)
- page the right evidence into the supervisor’s working context under strict budgets
- keep persistent conversation small

### 5) "More context" is transactional (Mount → Reason → Prune)

When the supervisor needs worker evidence:
- **Mount:** expand evidence pointers into full trace content at the LLM-call boundary (ephemeral, per-call).
- **Reason:** produce the user-facing answer using that evidence.
- **Prune:** commit only the final answer (and small provenance pointers); never persist raw mounted evidence into long-lived conversation state.

This is branch/rewind semantics: attach evidence to decide; don't commit evidence into memory.

Key insight: evidence mounting happens **before every LLM call within a run**, not just once at run start. The ReAct loop may involve multiple LLM calls (tool decisions → tool results → more decisions), and each call needs access to current evidence.

### 6) Require system invariants, not model behavior

We never require “the worker must summarize.” We require:
- **If any tool succeeded, evidence is preserved** (trace).
- **If evidence exists, it is mountable on demand** for the supervisor during the run (within budgets).
- **Long-lived conversation stays clean** (prune).

### 7) Workers are temporary; “no-workers” is the end state

Workers exist only because of:
- context-window limits
- latency/cost constraints
- messy I/O (logs, terminals) that would pollute the supervisor’s thread

As models improve and budgets expand, the worker boundary should collapse back into a single long-lived supervisor ReAct loop.

---

## Deterministic vs. inherently unstable (where to put contracts)

Classical engineering rule: put correctness contracts on the most deterministic surfaces.

In this system:
- **Deterministic:** tool calls + raw outputs, event timing/correlation, artifact persistence, budgets/truncation, ownership scoping.
- **Inherently unstable:** natural-language summaries, “final message” quality, what the model chooses to mention, formatting.

So the core contract becomes:
- Preserve and correlate evidence deterministically (trace).
- Make evidence available deterministically (mount).
- Keep long-lived state bounded deterministically (prune).

We do *not* attempt to make prose deterministic via schemas/parsers.

---

## Non-goals (explicit)

- No enforced schemas for worker outputs (no "must return JSON with fields X/Y/Z").
- No domain-specific parsing pipelines for command outputs (no regex-based disk/docker parsers).
- No permanent injection of raw logs/tool outputs into supervisor's long-lived thread.
- No "agent management bureaucracy" (workers exist to isolate context, not to build an organization chart).

**Clarification: execution metadata is allowed.** We distinguish between:
- ❌ Domain parsing: "parse df -h output into {filesystem, used_pct, ...}"
- ✅ Execution metadata: "tool ssh_exec ran, exit_code=0, duration=234ms, output_bytes=1847"

Execution metadata (tool index) helps prioritize evidence (failures first) without interpreting what the tool output *means*.

---

## Success criteria (what "good" looks like)

- The supervisor can reliably answer infrastructure questions even when worker prose is empty/garbage, because the supervisor can see the raw evidence at decision time.
- The supervisor's long-lived thread stays compact and useful; it does not accumulate tool dumps.
- Debugging is deterministic: "what happened" is a trace query, not a guess.
- Evidence is immediate: supervisor sees all available evidence at decision time without extra tool calls. No "dig in if curious" round-trips.

---

## Vocabulary

- **Trace:** append-only record of actions + outputs.
- **Mount:** transiently expand evidence pointers into full content at the LLM-call boundary.
- **Prune:** ensure mounted evidence does not become persistent conversation state.
- **Budget:** strict cap (tokens/bytes/lines/time) on mounted evidence.
- **Evidence compiler:** deterministic module that assembles evidence within budget, applying prioritization and truncation.
- **Tool index:** execution metadata (which tools ran, exit codes, durations) without domain-specific parsing.
- **Evidence marker:** pointer embedded in persisted messages that the evidence compiler expands at mount time.

---

## “Filesystem” mental model (optional but useful)

- Trace store ≈ journal/WAL (truth).
- Conversation store ≈ snapshot/commit (what we decided).
- Mounting ≈ overlay/union mount (ephemeral layer on top of snapshot).
- Pruning ≈ discard overlay; keep only new snapshot delta.
- Budgets ≈ page cache limits / buffer sizes (latency + memory control).
- Provenance pointers ≈ file handles/paths (how humans can re-open full evidence).
