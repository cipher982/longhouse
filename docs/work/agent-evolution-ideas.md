# Agent Evolution Strategy: Zerg (v2.2+)

This document captures architectural evolution ideas for Zerg, inspired by the "Aligning to What" (MiniMax M2) philosophy and adapted for a **Solo Developer / Ship V1** environment.

## Core Philosophy: "Trace-First Robustness"
Since Zerg has no QA team, we use the **Trace Store** (existing successful runs) as our primary source of truth for both performance optimization and regression testing.

---

## 1. Developer Productivity & "The Golden Loop"
**Goal:** Make the "check disk space on cube" loop (95% of dev runs) as fast and robust as possible.

### A. The "Golden Run" Replay Harness
- **Concept:** A script (`scripts/replay_run.py`) that loads a successful `run_id` from the DB and re-runs the Supervisor against its cached tool results.
- **Solo Dev Win:** Test Supervisor prompt changes in < 2 seconds without spinning up Docker or real workers. It’s "QA in a box" for a team of one.
- **V1 Move:** Implement a basic replay script that mocks the `spawn_worker` result.

### B. Invariant Monitoring
- **Concept:** Define "Performance Invariants" for common tasks.
- **Example:** *"Disk space checks must complete in ≤ 2 LLM round-trips."*
- **The Move:** Add a warning log if a run exceeds the step-count invariant, signaling that the Supervisor is getting "distracted" by new features.

---

## 2. Architectural Evolution (MiniMax Inspired)

### A. Interleaved Thinking (Non-blocking Reasoning)
- **Concept:** Move from a rigid "Wait-for-Tools" loop to a streaming model.
- **The Move:** Update the Supervisor ticker to stream "live reasoning" (using a cheap model like `gpt-4o-mini`) that analyzes worker logs as they arrive, rather than waiting for the worker to finish.
- **UX Impact:** Jarvis shows *"Worker found the log, looking for errors..."* instead of a generic spinner.

### B. Async Decisive Re-planning
- **Concept:** Allow the Supervisor to interrupt a batch of tool calls if one returns a decisive answer or critical error.
- **The Move:** Switch from `asyncio.gather` to `asyncio.as_completed` in the ReAct loop. If worker 1 fails critically, cancel workers 2 and 3 immediately.

### C. Perturbation Testing (Chaos Mode)
- **Concept:** Intentionally break tool outputs during dev/test to see if the Supervisor can recover.
- **The Move:** A "Chaos Resolver" that randomly injects transient errors (SSH timeouts, rate limits) into tool results.
- **Goal:** Verify "recovery logic" (like falling back from `runner_exec` to `ssh_exec`) without manually breaking servers.

---

## 3. Context & Evidence Optimization

### A. The "Stress Mount" Evidence Strategy
- **Concept:** During development, deliberately "stress" the Evidence Compiler by including messy, large, or out-of-order logs in the context window.
- **Goal:** Ensure the Supervisor is "aligned" to find the truth even when the budget forces aggressive truncation or the logs are noisy.

### B. Pre-Mounted Operational Space
- **Concept:** Avoid "discovery calls" (e.g., `list_runners`) by pre-injecting your server list (`cube`, `clifford`, etc.) into the Evidence Compiler's output.
- **Result:** Cut 1-2 round-trips out of every server-specific request.

---

## Implementation Backlog (High Leverage First)
1. [ ] **`scripts/replay_run.py`**: Mock harness for testing prompts against DB traces.
2. [ ] **Pre-mount server list**: Inject runner IDs directly from user context into the Evidence Compiler.
3. [ ] **Interleaved Heartbeat**: Upgrade the heartbeat events to include "Reasoning Chunks" from the Supervisor.
4. [ ] **Chaos Mode Resolver**: A flag to simulate tool failures for recovery testing.
