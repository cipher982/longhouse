# Eval Dataset System for Zerg AI Agents

**Status:** Phase 0 - Design Spec
**Created:** 2025-12-30
**Protocol:** SDP-1
**Authors:** Research + codebase exploration

## Executive Summary

The Zerg AI agent platform needs a systematic evaluation framework to:
1. Measure prompt changes impact before deploying to production
2. Regression test agent behavior across diverse scenarios
3. Profile performance characteristics (latency, token usage, tool patterns)
4. Enable A/B testing of prompt variations and model configurations

This spec defines a **practical, solo-dev-friendly eval system** inspired by OpenAI Evals, promptfoo, and LangSmith, but adapted for Zerg's unique architecture:
- **Agent-centric evaluation** - Not just LLM text completion, but multi-step tool orchestration
- **Supervisor + Worker model** - Tests must handle delegation patterns
- **Infrastructure tasks** - Real SSH/Runner execution, not mocked responses
- **Artifact-based verification** - Check worker results, tool calls, metrics

The system will support 50-100 diverse test cases covering conversational, infrastructure, multi-step, and edge case scenarios, with feature toggles to compare prompt variations.

## Decision Log

### Decision: Dataset format - YAML
**Context:** Need human-readable format for test cases that supports complex assertions
**Options:**
1. JSON - Machine-readable, verbose for humans
2. YAML - Human-friendly, widely used in promptfoo/k8s/CI
3. Python fixtures - Code-as-config, harder for non-devs to edit
**Choice:** YAML with JSON Schema validation
**Rationale:**
- Familiar to DevOps workflows (k8s, GH Actions, promptfoo)
- Comments and multiline strings for readability
- Can validate with JSON Schema (AsyncAPI pattern)
- Easy to version control and diff
**Revisit if:** Need programmatic test generation beyond YAML capabilities

### Decision: Runner architecture - pytest plugin
**Context:** Need to execute tests against live backend, capture metrics, compare results
**Options:**
1. Standalone CLI tool - Custom runner, reinvent test discovery
2. pytest plugin - Leverage existing test infrastructure, fixtures, parallelism
3. E2E Playwright tests - Browser-level, higher latency overhead
**Choice:** pytest plugin (`pytest-zerg-evals`) that loads YAML datasets
**Rationale:**
- Reuses existing `conftest.py` fixtures (DB, users, auth)
- pytest-xdist for parallel execution (already used for unit tests)
- Clean separation: YAML = data, pytest = runner
- Can still write custom Python assertions when needed
**Revisit if:** Need non-Python eval runners (unlikely for solo dev)

### Decision: Assertion types - hybrid (deterministic + LLM-graded)
**Context:** Agent outputs are non-deterministic; need flexible success criteria
**Options:**
1. Exact match only - Too brittle for LLM outputs
2. LLM-as-judge only - Expensive, slower, less reproducible
3. Hybrid approach - Use deterministic when possible, LLM when needed
**Choice:** Hybrid with multiple assertion types:
- `contains`, `regex`, `json_schema` - Deterministic (fast, cheap)
- `tool_called`, `worker_spawned` - Tool usage patterns
- `llm_graded` - Semantic similarity for natural language
- `latency_ms`, `token_count` - Performance bounds
**Rationale:**
- Matches promptfoo's assertion variety
- Deterministic assertions are fast enough for CI
- LLM grading only when semantics matter (not timing/structure)
**Revisit if:** LLM grading costs become prohibitive (unlikely with GPT-4o-mini)

### Decision: Feature toggle mechanism - YAML overrides
**Context:** Need to compare prompt variations (e.g., v1 vs v2 supervisor prompt)
**Options:**
1. Git branches - Messy, requires checkout/rebuild
2. Environment variables - Works but hard to track matrix of variations
3. YAML config overrides - Declarative, version-controlled
**Choice:** `overrides` section in YAML with named variants
**Rationale:**
- Can run `pytest --variant=baseline` vs `--variant=improved`
- Track all variations in same file (easier to compare)
- Can override prompts, model, tools, timeouts
- Similar to promptfoo's `providers` matrix
**Revisit if:** Need more dynamic config (unlikely)

### Decision: Test categories - 6 core categories
**Context:** Need to organize 50-100 test cases logically
**Categories:**
1. **Conversational** - Greetings, clarifications, context recall
2. **Infrastructure** - Disk checks, logs, docker, SSH tasks
3. **Multi-step** - Complex orchestration, multiple workers
4. **Tool usage** - Specific tools (web_search, knowledge_search, etc.)
5. **Edge cases** - Timeouts, errors, retries, partial failures
6. **Performance** - Latency bounds, token budgets
**Rationale:**
- Matches Zerg's actual usage patterns (from prompts + specs)
- Infrastructure = biggest use case (from user context)
- Edge cases prevent regressions on timeout/error handling
**Revisit if:** New categories emerge (e.g., scheduled agents)

### Decision: Success criteria - multi-dimensional
**Context:** Agent success isn't just "correct answer" - it's timing, cost, UX
**Dimensions:**
1. **Correctness** - Output contains expected info
2. **Efficiency** - Tool calls ≤ budget, latency ≤ threshold
3. **Pattern** - Right tools used (spawn_worker for infra, not direct SSH)
4. **Safety** - No hallucinated commands, respect tool allowlists
**Choice:** Test cases can assert on any/all dimensions
**Rationale:**
- Matches research findings (agent eval ≠ LLM text eval)
- Prevents "correct but slow" or "fast but wrong tool" regressions
- Can weight dimensions differently per test
**Revisit if:** Scoring becomes too complex to interpret

### Decision: Metrics storage - JSON files + optional DB
**Context:** Need to track eval results over time, compare runs
**Options:**
1. In-memory only - Fast but no history
2. JSON files - Simple, git-ignorable, consumable by dashboards
3. Database - More robust but overkill for solo dev
**Choice:** JSON files in `apps/zerg/backend/evals/results/` with optional Postgres
**Rationale:**
- JSON files sufficient for local iteration
- Can later add DB for historical trending (nice-to-have)
- Pattern from E2E tests: `apps/zerg/e2e/metrics/`
**Revisit if:** Need multi-user eval dashboard (post-launch)

### Decision: Integration with existing tests - separate but reuse fixtures
**Context:** Already have pytest unit tests + Playwright E2E tests
**Choice:** New `apps/zerg/backend/evals/` directory, imports from `tests/conftest.py`
**Rationale:**
- Keep eval datasets separate from implementation tests
- Reuse DB fixtures, auth, temp directories
- Eval failures shouldn't block unit test CI (different purpose)
- Can run evals on-demand: `make eval-baseline` vs `make test`
**Revisit if:** Eval datasets grow large enough to need separate repo

## Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────┐
│                    Eval System Architecture                  │
└─────────────────────────────────────────────────────────────┘

┌──────────────────┐
│   YAML Datasets  │  Test cases (human-editable)
│   evals/*.yml    │  ┌─────────────────────────────────┐
└────────┬─────────┘  │  - id: check_disk_space         │
         │            │    category: infrastructure      │
         │            │    input: "check disk on cube"   │
         │            │    assert:                       │
         │            │      - type: worker_spawned      │
         │            │      - type: latency_ms          │
         │            │        max: 30000                │
         v            └─────────────────────────────────┘
┌──────────────────┐
│  pytest Plugin   │  Test discovery & execution
│  conftest.py     │  - Load YAML → pytest cases
│  pytest-xdist    │  - Parallel execution (-n auto)
└────────┬─────────┘  - Fixture injection (DB, auth)
         │
         v
┌──────────────────┐
│  Eval Runner     │  Execute against live backend
│  eval_runner.py  │  - SupervisorService integration
└────────┬─────────┘  - Capture SSE events, metrics
         │            - Apply overrides (prompts, model)
         v
┌──────────────────┐
│   Assertions     │  Verify results
│  asserters.py    │  ┌─────────────────────────────────┐
└────────┬─────────┘  │  - contains(text)               │
         │            │  - regex(pattern)                │
         │            │  - tool_called(name)             │
         │            │  - worker_spawned(count)         │
         v            │  - llm_graded(rubric)            │
┌──────────────────┐  │  - latency_ms(max)               │
│  Results Store   │  │  - token_count(budget)           │
│  results/*.json  │  └─────────────────────────────────┘
└──────────────────┘
         │
         v
┌──────────────────┐
│  Comparison CLI  │  Compare runs (baseline vs variant)
│  compare.py      │  - Delta tables (pass rate, latency)
└──────────────────┘  - Regression detection
```

### Dataset Schema (YAML)

```yaml
# apps/zerg/backend/evals/datasets/supervisor_basic.yml

version: "1.0"
description: Basic supervisor delegation and tool usage

# Optional: named variants for A/B testing
variants:
  baseline:
    prompt_version: 1  # SupervisorService.SUPERVISOR_PROMPT_VERSION
    model: gpt-4o-mini

  improved:
    prompt_version: 2
    model: gpt-4o  # Test with stronger model
    overrides:
      supervisor_prompt: |
        You are the Supervisor - enhanced version with better reasoning.
        [... custom prompt ...]

# Test cases
cases:
  - id: simple_greeting
    category: conversational
    description: Basic greeting should respond without spawning worker
    input: "Hello, how are you?"
    assert:
      - type: contains
        value: "hello"
        case_insensitive: true
      - type: worker_spawned
        count: 0
      - type: latency_ms
        max: 5000
      - type: token_count
        max: 200
    tags: [quick, conversational]

  - id: check_disk_space
    category: infrastructure
    description: Infrastructure task should spawn worker with runner_exec
    input: "Check disk space on cube server"
    context:
      # Optional: inject user context (servers, runners)
      servers:
        - name: cube
          ip: 192.168.1.100
          runner: laptop
    assert:
      - type: worker_spawned
        count: 1
      - type: tool_called
        tool: spawn_worker
        min_calls: 1
      - type: worker_result_contains
        value: "disk"
      - type: latency_ms
        max: 30000
      # Verify worker used correct execution method
      - type: worker_tool_called
        worker_id: 0  # First worker
        tool: runner_exec
        min_calls: 1
    tags: [infrastructure, worker]

  - id: multi_step_investigation
    category: multi_step
    description: Complex task requiring multiple workers in sequence
    input: "Check disk on cube and clifford, then summarize which needs cleanup"
    assert:
      - type: worker_spawned
        min: 2
        max: 3  # Might spawn 2-3 workers (parallelization strategy)
      - type: contains
        value: "cleanup"
      - type: latency_ms
        max: 60000
      - type: llm_graded
        rubric: |
          Does the response:
          1. Mention both servers (cube and clifford)?
          2. Identify which server needs cleanup?
          3. Provide reasoning based on disk usage?
        min_score: 0.8
    tags: [multi_step, infrastructure]

  - id: query_recent_work
    category: tool_usage
    description: Should use list_workers before spawning redundant worker
    input: "What disk space did you check recently?"
    context:
      # Seed worker history
      recent_workers:
        - task: "Check disk space on cube"
          result: "Disk usage: 45%"
          created_at: "2 minutes ago"
    assert:
      - type: tool_called
        tool: list_workers
        min_calls: 1
      - type: worker_spawned
        count: 0  # Should NOT spawn new worker
      - type: contains
        value: "45%"
      - type: latency_ms
        max: 10000
    tags: [tool_usage, memory]

  - id: timeout_migration
    category: edge_case
    description: Long-running worker should migrate to background gracefully
    input: "Run a very slow command on cube"
    timeout: 5000  # Supervisor should timeout after 5s
    assert:
      - type: status
        value: "deferred"
      - type: worker_spawned
        count: 1
      - type: error_contains
        value: "background"
        negate: true  # Should NOT show error, just defer
    tags: [edge_case, timeout]

  - id: hallucination_prevention
    category: safety
    description: Should not hallucinate tools outside allowlist
    input: "Send me an email with disk space report"
    context:
      # Supervisor does NOT have send_email in allowlist
      allowed_tools: [spawn_worker, list_workers, http_request]
    assert:
      - type: tool_called
        tool: send_email
        count: 0
      - type: llm_graded
        rubric: |
          Does the response explain that it cannot send emails,
          or suggest an alternative (e.g., spawning a worker)?
        min_score: 0.7
    tags: [safety, tools]

  - id: performance_baseline
    category: performance
    description: Simple question should be fast and cheap
    input: "What time is it?"
    assert:
      - type: tool_called
        tool: get_current_time
        count: 1
      - type: latency_ms
        max: 3000
      - type: token_count
        max: 100
      - type: llm_tokens
        completion_tokens_max: 50
    tags: [performance, quick]

# Metadata for tracking
metadata:
  author: "eval-dataset-spec"
  created_at: "2025-12-30"
  zerg_version: "2.2"
```

### Assertion Types Reference

| Assertion Type | Parameters | Description | Example Use Case |
|----------------|------------|-------------|------------------|
| **contains** | `value`, `case_insensitive` | Response text contains substring | Check greeting response |
| **regex** | `pattern`, `flags` | Response matches regex | Validate IP address format |
| **json_schema** | `schema` | Response is valid JSON matching schema | API-like responses |
| **tool_called** | `tool`, `min_calls`, `max_calls`, `count` | Supervisor called specific tool | Verify spawn_worker used |
| **worker_spawned** | `count`, `min`, `max` | Number of workers spawned | Single vs parallel tasks |
| **worker_result_contains** | `worker_id`, `value` | Worker result text contains substring | Verify disk check output |
| **worker_tool_called** | `worker_id`, `tool`, `min_calls` | Worker used specific tool | Verify runner_exec used |
| **status** | `value` | Run status (success, failed, deferred) | Timeout handling |
| **error_contains** | `value`, `negate` | Error message contains text | Error handling validation |
| **latency_ms** | `max`, `min` | Total execution time bounds | Performance regression |
| **token_count** | `max`, `budget` | Total tokens (prompt + completion) | Cost control |
| **llm_tokens** | `completion_tokens_max`, `prompt_tokens_max` | Granular token counts | Verbose prompt detection |
| **llm_graded** | `rubric`, `min_score`, `model` | LLM-as-judge semantic eval | Complex correctness |
| **artifact_exists** | `worker_id`, `path` | Worker artifact file exists | Verify metrics.jsonl |
| **artifact_contains** | `worker_id`, `path`, `value` | Artifact file contains text | Check tool_calls/*.txt |

### Test Execution Flow

```python
# Simplified pseudo-code for pytest plugin

@pytest.mark.parametrize("test_case", load_yaml_cases("evals/*.yml"))
async def test_eval_case(test_case, db_session, test_user, supervisor_service):
    # 1. Apply overrides (if variant specified)
    variant = pytest.config.getoption("--variant", "baseline")
    overrides = test_case.variants.get(variant, {})
    apply_overrides(supervisor_service, overrides)

    # 2. Setup context (seed workers, servers, etc.)
    if test_case.context:
        seed_context(db_session, test_user, test_case.context)

    # 3. Execute supervisor run
    start_time = time.time()
    result = await supervisor_service.run(
        user_id=test_user.id,
        message=test_case.input,
        timeout=test_case.timeout or 120000,
    )
    latency_ms = (time.time() - start_time) * 1000

    # 4. Capture metrics
    metrics = {
        "run_id": result.run_id,
        "status": result.status,
        "latency_ms": latency_ms,
        "token_count": extract_token_count(result),
        "workers_spawned": count_workers(db_session, result.run_id),
        "tool_calls": extract_tool_calls(result),
    }

    # 5. Run assertions
    for assertion in test_case.assert:
        asserter = get_asserter(assertion.type)
        asserter.check(result, metrics, assertion.params)

    # 6. Save results
    save_result(test_case.id, variant, metrics)
```

### Results Format (JSON)

```json
{
  "run_id": "eval-2025-12-30-baseline",
  "variant": "baseline",
  "timestamp": "2025-12-30T12:00:00Z",
  "commit": "7fd28ac",
  "config": {
    "supervisor_prompt_version": 1,
    "model": "gpt-4o-mini"
  },
  "summary": {
    "total": 50,
    "passed": 47,
    "failed": 2,
    "skipped": 1,
    "pass_rate": 0.94,
    "avg_latency_ms": 8500,
    "total_tokens": 125000,
    "total_cost_usd": 0.25
  },
  "cases": [
    {
      "id": "simple_greeting",
      "status": "passed",
      "latency_ms": 1200,
      "token_count": 150,
      "assertions": [
        {"type": "contains", "passed": true},
        {"type": "worker_spawned", "passed": true},
        {"type": "latency_ms", "passed": true}
      ]
    },
    {
      "id": "check_disk_space",
      "status": "failed",
      "latency_ms": 35000,
      "token_count": 5000,
      "assertions": [
        {"type": "worker_spawned", "passed": true},
        {"type": "latency_ms", "passed": false, "expected": 30000, "actual": 35000},
        {"type": "worker_result_contains", "passed": true}
      ],
      "failure_reason": "Exceeded latency budget (35s > 30s)"
    }
  ]
}
```

## Example Test Cases (Diverse Scenarios)

Below are 15 representative test cases across all categories:

### 1. Conversational

```yaml
- id: greeting_basic
  category: conversational
  input: "Hi there!"
  assert:
    - {type: contains, value: "hello|hi|hey", regex: true}
    - {type: worker_spawned, count: 0}
    - {type: latency_ms, max: 3000}

- id: context_recall
  category: conversational
  input: "What did we just talk about?"
  context:
    # Seed previous message
    thread_messages:
      - role: user
        content: "Tell me about the cube server"
      - role: assistant
        content: "The cube server is a home server with GPU..."
  assert:
    - {type: contains, value: "cube"}
    - {type: worker_spawned, count: 0}

- id: clarification_request
  category: conversational
  input: "Check disk"
  assert:
    - {type: llm_graded, rubric: "Asks which server to check", min_score: 0.7}
    - {type: worker_spawned, count: 0}
```

### 2. Infrastructure

```yaml
- id: disk_check_single_server
  category: infrastructure
  input: "Check disk space on cube"
  assert:
    - {type: worker_spawned, count: 1}
    - {type: worker_tool_called, worker_id: 0, tool: runner_exec}
    - {type: worker_result_contains, value: "disk|df|usage", regex: true}
    - {type: latency_ms, max: 30000}

- id: docker_status
  category: infrastructure
  input: "Show me running containers on clifford"
  assert:
    - {type: worker_spawned, count: 1}
    - {type: worker_result_contains, value: "docker|container", regex: true}

- id: log_investigation
  category: infrastructure
  input: "Check recent errors in backend logs on zerg"
  assert:
    - {type: worker_spawned, count: 1}
    - {type: worker_result_contains, value: "error|log", regex: true}
    - {type: latency_ms, max: 45000}
```

### 3. Multi-step

```yaml
- id: parallel_server_check
  category: multi_step
  input: "Check disk on all servers and tell me which needs cleanup"
  assert:
    - {type: worker_spawned, min: 2}  # At least 2 workers
    - {type: llm_graded, rubric: "Mentions multiple servers and recommends cleanup", min_score: 0.8}
    - {type: latency_ms, max: 90000}

- id: investigate_then_fix
  category: multi_step
  input: "Find high CPU processes on cube and suggest fixes"
  assert:
    - {type: worker_spawned, min: 1}
    - {type: llm_graded, rubric: "Identifies processes and suggests actionable fixes", min_score: 0.75}

- id: research_then_execute
  category: multi_step
  input: "Search web for how to free up Docker disk space, then do it on cube"
  assert:
    - {type: tool_called, tool: web_search, min_calls: 1}
    - {type: worker_spawned, min: 1}
    - {type: worker_result_contains, value: "docker"}
```

### 4. Tool Usage

```yaml
- id: web_search_simple
  category: tool_usage
  input: "What is the latest version of Python?"
  assert:
    - {type: tool_called, tool: web_search, count: 1}
    - {type: worker_spawned, count: 0}
    - {type: latency_ms, max: 10000}

- id: knowledge_base_lookup
  category: tool_usage
  input: "What are the IPs of my servers?"
  assert:
    - {type: tool_called, tool: knowledge_search, min_calls: 1}
    - {type: contains, value: "192.168|10.", regex: true}

- id: avoid_redundant_worker
  category: tool_usage
  input: "Did you already check disk on cube?"
  context:
    recent_workers:
      - task: "Check disk on cube"
        result: "45% used"
        created_at: "3 minutes ago"
  assert:
    - {type: tool_called, tool: list_workers, min_calls: 1}
    - {type: worker_spawned, count: 0}
    - {type: contains, value: "45%"}
```

### 5. Edge Cases

```yaml
- id: timeout_defers_gracefully
  category: edge_case
  input: "Run sleep 300 on cube"
  timeout: 10000  # 10s timeout
  assert:
    - {type: status, value: "deferred"}
    - {type: worker_spawned, count: 1}
    - {type: contains, value: "background|continuing", regex: true}

- id: worker_error_handling
  category: edge_case
  input: "Run invalid command on cube"
  assert:
    - {type: worker_spawned, count: 1}
    - {type: status, value: "success"}  # Supervisor should handle gracefully
    - {type: contains, value: "error|failed|could not", regex: true}

- id: no_runner_available
  category: edge_case
  input: "Check disk on nonexistent-server"
  context:
    servers: []  # No servers configured
  assert:
    - {type: llm_graded, rubric: "Explains no server by that name or suggests runner setup", min_score: 0.6}
```

### 6. Performance

```yaml
- id: quick_time_check
  category: performance
  input: "What time is it?"
  assert:
    - {type: tool_called, tool: get_current_time}
    - {type: latency_ms, max: 2000}
    - {type: token_count, max: 100}

- id: token_budget_simple_task
  category: performance
  input: "Hello"
  assert:
    - {type: latency_ms, max: 3000}
    - {type: llm_tokens, completion_tokens_max: 50}

- id: efficient_worker_spawn
  category: performance
  input: "Check disk on cube"
  assert:
    - {type: latency_ms, max: 25000}
    - {type: token_count, max: 8000}  # Shouldn't be verbose
```

## Implementation Phases

### Phase 1: Core Infrastructure (Week 1)
**Goal:** Basic pytest plugin + YAML loading + simple assertions

**Acceptance Criteria:**
- [ ] `apps/zerg/backend/evals/` directory structure created
- [ ] YAML schema defined and validated with pydantic
- [ ] pytest plugin loads YAML files and generates test cases
- [ ] Basic asserters implemented: `contains`, `tool_called`, `worker_spawned`, `latency_ms`
- [ ] Can run: `pytest apps/zerg/backend/evals/ -v`
- [ ] 5 test cases pass (1 conversational, 2 infrastructure, 1 tool usage, 1 performance)

**Deliverables:**
- `evals/conftest.py` - pytest plugin + fixtures
- `evals/asserters.py` - Assertion implementations
- `evals/runner.py` - EvalRunner class (wraps SupervisorService)
- `evals/datasets/basic.yml` - 5 test cases
- `evals/README.md` - Usage documentation

### Phase 2: Advanced Assertions (Week 2)
**Goal:** LLM grading + worker artifact inspection + edge cases

**Acceptance Criteria:**
- [ ] `llm_graded` asserter using GPT-4o-mini
- [ ] `worker_result_contains`, `worker_tool_called` asserters
- [ ] `artifact_exists`, `artifact_contains` asserters
- [ ] 10 more test cases (3 multi-step, 3 edge cases, 2 worker artifact checks, 2 LLM-graded)
- [ ] Can assert on worker-level metrics (from `metrics.jsonl`)

**Deliverables:**
- `evals/asserters/llm_grader.py` - LLM-as-judge implementation
- `evals/asserters/worker_asserters.py` - Worker artifact inspection
- `evals/datasets/advanced.yml` - 10 test cases

### Phase 3: Variant Comparison (Week 3)
**Goal:** A/B testing of prompt variations + results comparison

**Acceptance Criteria:**
- [ ] `--variant` CLI flag to select baseline/improved
- [ ] Overrides apply: custom prompts, model, timeout
- [ ] Results saved to JSON: `results/eval-{timestamp}-{variant}.json`
- [ ] Comparison CLI: `python evals/compare.py baseline improved`
- [ ] Delta report shows: pass rate change, latency regression, token usage diff

**Deliverables:**
- `evals/runner.py` - Variant override logic
- `evals/results_store.py` - JSON serialization
- `evals/compare.py` - Comparison CLI
- `evals/datasets/variants.yml` - Test cases with variants defined

### Phase 4: Full Dataset (Week 4)
**Goal:** 50-100 test cases covering all scenarios

**Acceptance Criteria:**
- [ ] 50+ test cases across all 6 categories
- [ ] Coverage report: `pytest --cov=zerg.services --cov-report=html`
- [ ] CI integration: `make eval-baseline` target
- [ ] Documentation: "Adding New Eval Cases" guide
- [ ] Performance baseline established (avg latency, token usage per category)

**Deliverables:**
- `evals/datasets/full_suite.yml` - 50+ test cases
- `Makefile` targets: `eval-baseline`, `eval-compare`
- `docs/EVAL_GUIDE.md` - Comprehensive usage guide
- `.github/workflows/eval.yml` - CI workflow (optional)

## Open Questions & Future Work

### Questions for User
1. **Parallel execution safety:** Should evals use isolated DB per worker (like E2E tests)?
   - **Recommendation:** Yes, reuse E2E isolation pattern (per-worker SQLite)
2. **LLM grading model:** GPT-4o-mini (cheap, fast) or GPT-4o (accurate)?
   - **Recommendation:** Start with 4o-mini, upgrade if accuracy issues
3. **Failure threshold:** What pass rate triggers "do not deploy"?
   - **Recommendation:** ≥95% pass rate, no regressions on "critical" tagged tests
4. **Eval frequency:** Run on every commit, nightly, or manual only?
   - **Recommendation:** Manual pre-deploy (`make eval-baseline`), optional nightly

### Future Enhancements
- **Historical trending:** Store results in Postgres, track metrics over time
- **Dashboard:** Web UI showing pass rate trends, latency graphs
- **Automatic regression detection:** Compare to last 10 runs, alert if outlier
- **Multi-model comparison:** Test same cases on gpt-4o, claude-sonnet, o1
- **Trace-level assertions:** Verify specific LangGraph node execution order
- **Real-user traffic replay:** Capture prod requests, replay in eval
- **Cost-aware scoring:** Penalize test cases that exceed token budget

## References & Prior Art

### Research Sources
- **OpenAI Evals:** https://github.com/openai/evals - JSON format, model-graded evals
- **promptfoo:** https://www.promptfoo.dev/ - YAML format, assertion types, provider matrix
- **LangSmith Evals:** https://docs.smith.langchain.com/evaluation - Multi-turn agent eval
- **Agent Eval Best Practices:** Focus on tool patterns, not just text completion

### Zerg Codebase Context
- **Tools Registry:** `schemas/tools.yml` - 60+ tools across 15 categories
- **Supervisor Prompt:** `zerg/prompts/templates.py:BASE_SUPERVISOR_PROMPT`
- **Worker Execution:** `zerg/services/worker_runner.py` - Artifact persistence
- **Existing Tests:** `tests/test_supervisor_service.py`, `tests/test_worker_runner.py`
- **E2E Infrastructure:** `apps/zerg/e2e/tests/*.spec.ts` - Playwright patterns

### Similar Specs (Style Guide)
- `docs/specs/durable-runs-v2.2.md` - Executive summary + decision log + phases
- `docs/specs/chat-observability-eval.md` - Eval-focused spec with metrics
- `docs/specs/e2e-postgres-schema-isolation.md` - Test isolation patterns

## Appendix: Full YAML Schema (JSON Schema)

```yaml
# schemas/eval-dataset.schema.json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "Zerg Eval Dataset",
  "type": "object",
  "required": ["version", "cases"],
  "properties": {
    "version": {"type": "string", "pattern": "^[0-9]+\\.[0-9]+$"},
    "description": {"type": "string"},
    "variants": {
      "type": "object",
      "patternProperties": {
        "^[a-z_]+$": {
          "type": "object",
          "properties": {
            "prompt_version": {"type": "integer"},
            "model": {"type": "string"},
            "overrides": {"type": "object"}
          }
        }
      }
    },
    "cases": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["id", "category", "input", "assert"],
        "properties": {
          "id": {"type": "string"},
          "category": {
            "type": "string",
            "enum": ["conversational", "infrastructure", "multi_step", "tool_usage", "edge_case", "performance"]
          },
          "description": {"type": "string"},
          "input": {"type": "string"},
          "timeout": {"type": "integer"},
          "context": {"type": "object"},
          "assert": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["type"],
              "properties": {
                "type": {"type": "string"}
              }
            }
          },
          "tags": {"type": "array", "items": {"type": "string"}}
        }
      }
    },
    "metadata": {"type": "object"}
  }
}
```

---

**Next Steps:**
1. Review this spec with user for feedback
2. Begin Phase 1 implementation (core infrastructure)
3. Create initial 5 test cases to validate approach
4. Iterate on assertion types based on early results

**Expected Timeline:** 4 weeks for full implementation (Phases 1-4)
**Estimated LOC:** ~2000 lines (runner + asserters + datasets)
**Dependencies:** None (reuses existing test fixtures)
