# Multi-Agent Research System Improvements

**Status:** Spec Complete | Implementation Pending
**Created:** 2025-01-02
**Source:** Analysis of [Anthropic's Multi-Agent Research System](https://www.anthropic.com/engineering/multi-agent-research-system) (June 2025)

---

## Executive Summary

Zerg's supervisor/worker architecture is **closer to a research-grade multi-agent system than expected**, but has critical gaps that silently break features in production. This spec documents the gaps, proposes fixes, and establishes a testing strategy to prevent future regressions.

### Key Findings

1. **Evidence mounting exists but is broken in production** - The continuation path doesn't include evidence markers
2. **Multi-worker join is missing** - Continuation is 1:1; parallel workers can't converge properly
3. **No citation pipeline** - Sources exist in tool outputs but aren't structured for UI
4. **Worker prompts optimize for infra, not research** - "Minimum necessary steps" vs "cite your sources"
5. **Tests pass while features are broken** - Unit tests mock too much; E2E tests assert too little

---

## Part 1: Architecture Gaps

### 1.1 Evidence Mounting Not Wired in Production

**The Bug:**

Evidence markers are generated in `roundabout_monitor.py:1094` (the `wait=True` eval path), but production uses fire-and-forget workers with durable continuation. The continuation flow at `supervisor_service.py:848` injects a plain text message WITHOUT the evidence marker:

```python
# Current (broken):
tool_result_content = f"[Worker job {job_id} completed]\n\n" \
                      f"Worker ID: {worker_id}\n" \
                      f"Result:\n{result_summary}"
# Missing: [EVIDENCE:run_id=X,job_id=Y,worker_id=Z]
```

**Impact:**
- `EvidenceMountingLLM` wrapper never expands evidence during production synthesis
- Supervisor only sees `result_summary` (often truncated/lossy)
- Full tool outputs from workers are inaccessible during final answer generation

**Fix:** One-line change to add marker:

```python
# Fixed:
tool_result_content = f"[Worker job {job_id} completed]\n\n" \
                      f"Worker ID: {worker_id}\n" \
                      f"Result:\n{result_summary}\n\n" \
                      f"[EVIDENCE:run_id={original_run_id},job_id={job_id},worker_id={worker_id}]"
```

**File:** `apps/zerg/backend/zerg/services/supervisor_service.py:848`

---

### 1.2 Multi-Worker Join Missing ("Chord" Pattern)

**The Bug:**

The unique constraint on `continuation_of_run_id` (`models/run.py:86-95`) enforces exactly one continuation per original run:

```python
Index(
    "ix_agent_runs_unique_continuation",
    continuation_of_run_id,
    unique=True,
    postgresql_where=(continuation_of_run_id.isnot(None)),
)
```

When supervisor spawns 5 workers in parallel:
1. Worker 1 completes → triggers continuation
2. Workers 2-5 complete → fast-path returns existing continuation (no additional messages)

Evidence mounting *could* compensate (queries all workers), but since production doesn't use markers, workers 2-5's results aren't reliably available.

**Impact:**
- Parallel research queries can't properly converge
- Only first worker's result is explicitly injected
- "Research X, Y, and Z in parallel" workflows are unreliable

**Proposed Fix:** Add worker group tracking

Option A: Simple counter
```python
# Add to AgentRun model:
pending_workers: int = Column(Integer, default=0)

# On spawn: increment
# On complete: decrement
# Only trigger continuation when pending_workers == 0
```

Option B: `wait_for_workers` tool
```python
@tool(name="wait_for_workers")
async def wait_for_workers(run_ids: list[str], timeout_seconds: int = 300) -> dict:
    """Wait for multiple worker runs to complete, then return their results."""
    # Poll until all complete or timeout
    # Return aggregated results with evidence markers
```

**Recommendation:** Option A is simpler (~50 LOC), Option B is more flexible. Start with A.

---

### 1.3 No Citation Pipeline

**Current State:**
- `web_search` returns: `{title, url, content, score, published_date}`
- `web_fetch` returns: `{url, content, word_count}`
- Sources exist in tool outputs but no structured citation tracking

**What Anthropic Does:**
- Dedicated `CitationAgent` that aligns claims → sources after synthesis
- "Source pack" artifact the UI can render

**Proposed Fix:**

1. **Structured source metadata in tool outputs** (low effort)
   - Already present, just needs consistent format

2. **Citation extraction prompt** (medium effort)
   - Add second LLM pass after synthesis
   - Input: final response + all source URLs from evidence
   - Output: `[{claim, source_url, excerpt}]`

3. **UI citation panel** (medium effort)
   - Render structured citations alongside response
   - Click to expand source excerpt

**Recommendation:** Start with consistent source metadata. Add citation extraction when evidence mounting works.

---

### 1.4 Worker Prompts Optimized for Infra, Not Research

**Current Worker Prompt** (`templates.py:226-270`):
```
Your goal is to achieve the user's objective with the **minimum necessary steps**.

For simple checks (disk, memory, processes, docker):
Aim for ONE command, then DONE.
```

This is perfect for infra tasks but wrong for research.

**Proposed Addition:** `RESEARCH_WORKER_PROMPT`

```python
RESEARCH_WORKER_PROMPT = """You are a Research Worker - you gather information and cite sources.

## Goal-Oriented Research

Your goal is to find accurate, well-sourced information.

**For research tasks:**
- Use web_search to find relevant sources
- Use web_fetch to read full content from promising URLs
- ALWAYS cite your sources with URLs
- Prefer authoritative sources (official docs, reputable publications)
- Include direct quotes when relevant

**Response Format:**
Summary of findings with [source](url) citations inline.

Example:
"Python 3.12 introduced improved error messages [source](https://docs.python.org/3.12/whatsnew)
and a new type parameter syntax [source](https://peps.python.org/pep-0695/)."

## Available Servers
{servers}

## Additional Context
{user_context}
"""
```

**Implementation:**
- Add prompt to `templates.py`
- Add `worker_type` parameter to `spawn_worker`
- Supervisor requests `worker_type="research"` for research tasks

---

## Part 2: Testing Strategy

### 2.1 Why Current Tests Miss These Bugs

| Test Type | LLM | What It Tests | Why It Missed the Bug |
|-----------|-----|---------------|----------------------|
| Unit tests | Mocked | API contracts, DB | Never runs real continuation flow |
| Evals (hermetic) | Stubbed | Infra routing | Tests roundabout path, not production |
| Evals (live) | Real | Agent behavior | Backend only, no UI |
| Playwright E2E | Real | UI loads | Weak assertions ("contains text") |

**The Gap:** No test verifies "user asks question → worker runs → evidence used in synthesis → user sees correct answer."

---

### 2.2 Proposed Test Taxonomy

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  TIER 1: UNIT TESTS (fast, mocked, every commit)                           │
│  ├── Backend unit tests (existing)                                          │
│  ├── Frontend unit tests (existing)                                         │
│  └── Purpose: Catch regressions in business logic                           │
│                                                                             │
│  TIER 2: INTEGRATION TESTS (mocked LLM, real DB)                           │
│  ├── Evals hermetic (existing)                                              │
│  ├── Purpose: Catch infra/wiring bugs                                       │
│  └── NEW: Test continuation includes evidence marker                        │
│                                                                             │
│  TIER 3: AGENT TESTS (real LLM, no UI)                                     │
│  ├── Evals live (existing, expand)                                          │
│  ├── LLM-judged assertions                                                  │
│  └── Purpose: Catch agent behavior bugs                                     │
│                                                                             │
│  TIER 4: E2E JOURNEY TESTS (Playwright + real LLM + LLM-judged)            │
│  ├── NEW: user-journeys.spec.ts                                            │
│  └── Purpose: Catch user-facing bugs                                        │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

### 2.3 LLM-as-Judge Best Practices

From 2025/2026 research (RISE-Judge, Agent-as-a-Judge):

**1. Judge the Trace, Not Just the Answer**
```typescript
// BAD: Only checks final response
await expect(response).toContainText(/disk/i);

// GOOD: Checks tool calls + evidence + response
const result = await traceJudge({
  events: sseEvents,
  finalResponse: response,
  rubric: [
    { id: 'correct_tool', question: 'Was ssh_exec used?' },
    { id: 'evidence_used', question: 'Does response quote tool output?' },
  ]
});
```

**2. Checklist Rubrics > 1-10 Scales**
```typescript
// BAD: Vague scale
"Rate the response quality 1-10"

// GOOD: Binary checklist
[
  { question: 'Contains specific disk usage numbers?', weight: 1 },
  { question: 'Numbers are specific (45%), not vague (some)?', weight: 1 },
  { question: 'Mentions which server was checked?', weight: 1 },
]
```

**3. Model Diversity for Judging**
- Use Claude to judge GPT outputs (or vice versa)
- Avoids self-enhancement bias

**4. Force Chain-of-Thought Before Score**
- Reduces stochasticity
- Judge writes reasoning first, then gives verdict

**5. Tiered Judging**
- gpt-4o-mini for 90% of routine checks (~$0.01 per run)
- Escalate ambiguous/high-stakes to frontier model

---

### 2.4 Specific Tests to Add

**Tier 2: Integration (catches evidence marker bug)**
```python
def test_continuation_includes_evidence_marker():
    """Verify production continuation path includes evidence marker."""
    # Create DEFERRED run
    run = create_run(status=RunStatus.DEFERRED)

    # Trigger continuation
    await trigger_continuation(run.id, job_id=1, worker_id="test", result="done")

    # Assert marker present
    tool_msg = get_last_tool_message(run.thread_id)
    assert "[EVIDENCE:" in tool_msg.content
```

**Tier 3: Agent (catches reasoning bugs)**
```yaml
# evals/datasets/live.yml
- id: evidence_used_in_synthesis
  category: behavior
  description: Supervisor uses worker evidence in final answer
  input: "Run hostname command on cube and tell me the exact output"
  timeout: 120
  assert:
    - type: worker_spawned
      min: 1
    - type: llm_graded
      rubric: |
        1. Response contains actual hostname (not hallucinated)
        2. Response does NOT say "I couldn't run the command"
        3. Response appears to quote real tool output
      min_score: 0.9
  tags: [critical, behavior]
```

**Tier 4: E2E Journey (catches user-facing bugs)**
```typescript
// apps/zerg/e2e/tests/user-journeys.spec.ts
test('user checks disk space and sees real data', async ({ page }) => {
  await page.goto('/chat');
  await page.fill('.text-input', 'Check disk space on cube');
  await page.click('.send-button');

  await page.waitForSelector('.message.assistant', { timeout: 120_000 });
  const response = await page.locator('.message.assistant').last().innerText();

  const result = await llmJudge({
    task: 'Check disk space on cube',
    response,
    rubric: `
      1. Contains specific disk usage numbers (percentages or GB)
      2. Numbers are specific (like "45%"), not vague ("some space")
      3. Does NOT say "I couldn't check" or similar failure
    `,
    minScore: 0.8,
  });

  expect(result.passed, result.reason).toBe(true);
});
```

---

## Part 3: Implementation Tasks

### Phase 1: Critical Fixes (High Impact, Low Effort)

- [ ] **Add evidence marker to continuation** (`supervisor_service.py:848`)
  - 1 line change
  - Unblocks evidence mounting in production
  - Priority: P0

- [ ] **Add integration test for evidence marker**
  - Catches if this regresses
  - Priority: P0

### Phase 2: Multi-Worker Support

- [ ] **Add `pending_workers` counter to AgentRun**
  - Schema change + migration
  - ~50 LOC
  - Priority: P1

- [ ] **Update spawn_worker to increment counter**
  - Priority: P1

- [ ] **Update worker completion to decrement + check**
  - Only trigger continuation when counter == 0
  - Priority: P1

- [ ] **Add multi-worker integration test**
  - Spawn 3 workers, verify all results in synthesis
  - Priority: P1

### Phase 3: Research Workflow

- [ ] **Add RESEARCH_WORKER_PROMPT to templates.py**
  - ~30 LOC
  - Priority: P2

- [ ] **Add `worker_type` parameter to spawn_worker**
  - Priority: P2

- [ ] **Update supervisor prompt with research patterns**
  - When to use research workers
  - How to synthesize multiple sources
  - Priority: P2

### Phase 4: Citation Pipeline

- [ ] **Standardize source metadata in tool outputs**
  - Ensure web_search/web_fetch return consistent structure
  - Priority: P3

- [ ] **Add citation extraction prompt**
  - Second pass after synthesis
  - Output: `[{claim, source_url, excerpt}]`
  - Priority: P3

- [ ] **UI citation panel**
  - Render citations alongside response
  - Priority: P3

### Phase 5: Testing Infrastructure

- [ ] **Create `llmJudge()` helper for Playwright**
  - `apps/zerg/e2e/lib/llm-judge.ts`
  - Priority: P1

- [ ] **Create `traceJudge()` for SSE event evaluation**
  - Judge tool calls + evidence + response together
  - Priority: P2

- [ ] **Add user journey test suite**
  - `apps/zerg/e2e/tests/user-journeys.spec.ts`
  - 5-10 core journeys with LLM-judged assertions
  - Priority: P1

- [ ] **Expand live evals with behavior tests**
  - Evidence usage, multi-worker synthesis, citations
  - Priority: P2

---

## Part 4: Success Criteria

### Evidence Mounting Works
- [ ] Production continuation includes `[EVIDENCE:...]` marker
- [ ] `EvidenceMountingLLM` expands evidence during synthesis
- [ ] Test verifies marker presence in continuation path

### Multi-Worker Join Works
- [ ] Supervisor can spawn N workers in parallel
- [ ] Continuation triggers only after ALL workers complete
- [ ] Final synthesis includes results from ALL workers
- [ ] Test verifies N workers → N results in synthesis

### Research Quality Improved
- [ ] Research workers cite sources with URLs
- [ ] Responses include inline citations
- [ ] Test verifies citations present in research responses

### Testing Catches Bugs
- [ ] Evidence marker regression would fail Tier 2 test
- [ ] Bad agent reasoning would fail Tier 3 test
- [ ] Broken user experience would fail Tier 4 test

---

## Appendix: Reference Materials

### Anthropic Multi-Agent Research System (Key Takeaways)

1. **Orchestrator + Subagents** - Lead agent decomposes, spawns parallel subagents, synthesizes
2. **"Search is Compression"** - Subagents run in parallel, return distilled findings
3. **Explicit Effort Scaling** - Budget hints (how many subagents/tool calls) based on complexity
4. **Tool UX Matters** - Tool descriptions are product UX
5. **Citations as Dedicated Step** - Separate pass aligns claims → sources
6. **Evals are Outcome-Focused** - LLM-as-judge with rubrics

### Files Referenced

| File | Purpose |
|------|---------|
| `apps/zerg/backend/zerg/services/supervisor_service.py:848` | Continuation message injection |
| `apps/zerg/backend/zerg/services/evidence_mounting_llm.py` | Evidence expansion wrapper |
| `apps/zerg/backend/zerg/services/roundabout_monitor.py:1094` | Evidence marker generation |
| `apps/zerg/backend/zerg/models/run.py:86-95` | Unique constraint on continuation |
| `apps/zerg/backend/zerg/prompts/templates.py` | Supervisor/worker prompts |
| `apps/zerg/backend/evals/asserters.py:454` | `assert_llm_graded` |

---

## Changelog

- **2025-01-02:** Initial spec created from Claude Code session analysis
