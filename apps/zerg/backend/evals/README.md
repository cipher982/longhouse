# Zerg Eval Dataset System

A comprehensive evaluation framework for AI agent quality, regression testing, and prompt optimization.

**Status:** Phase 4 Complete (53 test cases, tag filtering, deployment gates)

## Quick Start

### Run All Tests (Hermetic Mode)
```bash
make eval                # All tests (hermetic mode, no real LLM)
```

### Run Filtered Tests
```bash
make eval-critical       # Critical tests only (deployment gate)
make eval-fast           # Fast tests only (< 5s each)
make eval-all            # All tests including slow ones
```

### Run Live Tests (Real OpenAI)
```bash
make eval-live           # Live mode with real OpenAI API
```

### Compare Variants
```bash
make eval EVAL_VARIANT=baseline
make eval EVAL_VARIANT=improved
make eval-compare BASELINE=eval-2025-12-30-baseline-abc123.json VARIANT=eval-2025-12-30-improved-def456.json
```

## Test Categories

The eval suite includes **53 test cases** across 6 categories:

| Category | Count | Description |
|----------|-------|-------------|
| **Conversational** | 9 | Greetings, clarifications, status checks |
| **Infrastructure** | 14 | Disk, CPU, memory, Docker, services |
| **Multi-step** | 7 | Complex workflows, parallel tasks, dependencies |
| **Tool usage** | 9 | Tool selection, multiple tools, knowledge base |
| **Edge cases** | 7 | Unicode, special chars, malformed input |
| **Performance** | 7 | Latency bounds, token efficiency |

## Tag System

Tests are tagged for flexible filtering:

| Tag | Meaning | Usage |
|-----|---------|-------|
| **critical** | Must pass for deployment | `pytest -m critical` |
| **fast** | < 5s execution time | `pytest -m fast` |
| **slow** | > 30s execution time | `pytest -m slow` |
| **optional** | Informational, no block | `pytest -m optional` |
| **quick** | Quick sanity check | `pytest -m quick` |

Plus category tags: `conversational`, `infrastructure`, `multi_step`, `tool_usage`, `edge_case`, `performance`, `worker`, `multi_turn`

### Filter Examples
```bash
# Critical tests only
pytest evals/ -m critical

# Fast tests only
pytest evals/ -m fast

# Fast AND critical
pytest evals/ -m "fast and critical"

# Infrastructure tests excluding slow ones
pytest evals/ -m "infrastructure and not slow"

# All conversational tests
pytest evals/ -m conversational
```

## Adding New Eval Cases

### 1. Choose the Right Dataset

- **basic.yml** - Hermetic mode tests (53 cases)
- **live.yml** - Live mode tests with LLM grading (2 cases)

### 2. Add a Test Case

Edit `evals/datasets/basic.yml`:

```yaml
cases:
  - id: your_test_id                    # Unique identifier
    category: infrastructure            # Category (see above)
    description: What this test checks  # Human-readable description
    input: "Your task here"             # Single-turn input
    timeout: 60                         # Timeout in seconds
    assert:
      - type: status
        value: success
      - type: latency_ms
        max: 15000
      - type: worker_spawned
        count: 1
    tags: [infrastructure, critical]    # Tags for filtering
```

### 3. Multi-turn Conversations

For multi-turn tests, use `messages` instead of `input`:

```yaml
  - id: multi_turn_example
    category: conversational
    description: Multi-turn conversation test
    messages:
      - role: user
        content: "First message"
      - role: assistant
        content: "Response to first message"
      - role: user
        content: "Follow-up question"
    timeout: 30
    assert:
      - type: status
        value: success
    tags: [conversational, multi_turn]
```

### 4. Supported Assertions

| Assertion | Parameters | Description |
|-----------|------------|-------------|
| **contains** | `value`, `case_insensitive` | Text contains substring |
| **regex** | `pattern`, `flags` | Regex match |
| **status** | `value` | success/failed/deferred |
| **latency_ms** | `max`, `min` | Execution time bounds |
| **total_tokens** | `max` | Token usage |
| **worker_spawned** | `count`, `min`, `max` | Workers spawned |
| **tool_called** | `value` | Tool was called |
| **worker_result_contains** | `worker_id`, `value` | Worker result text |
| **worker_tool_called** | `worker_id`, `value`, `min` | Worker used tool |
| **artifact_exists** | `worker_id`, `value` | Artifact file exists |
| **artifact_contains** | `worker_id`, `path`, `value` | Artifact content |
| **llm_graded** | `rubric`, `min_score` | LLM-as-judge (live mode) |

### 5. Tag Your Test

Choose appropriate tags:

**Priority:**
- `critical` - Must pass for deployment (use sparingly!)
- `fast` - Quick test (< 5s)
- `slow` - Long test (> 30s)
- `optional` - Informational only

**Category:**
- `conversational`, `infrastructure`, `multi_step`, `tool_usage`, `edge_case`, `performance`

**Special:**
- `worker` - Tests worker delegation
- `multi_turn` - Multi-turn conversation
- `quick` - Quick sanity check

### 6. Run Your Test

```bash
# Run just your new test
pytest evals/ -k "your_test_id"

# Run with all tests in the category
make eval
```

## Hermetic vs Live Mode

### Hermetic Mode (Default)
- **LLM:** Stubbed with deterministic responses
- **Tools:** Safe subset only (no real SSH/destructive actions)
- **Speed:** Fast (~2s per test)
- **Cost:** Free (no API calls)
- **Use:** CI, fast iteration, infrastructure testing

### Live Mode (Opt-in)
- **LLM:** Real OpenAI API calls
- **Tools:** Full supervisor toolset
- **Speed:** Slower (~10-30s per test)
- **Cost:** Real API costs
- **Use:** Pre-deploy validation, prompt quality testing

## Deployment Gate

Critical tests act as a **deployment gate**:

```bash
make eval-critical
```

**Rules:**
- All critical tests MUST pass 100%
- Failures block deployment to production
- Use critical tag sparingly (currently 7 tests)

**Current critical tests:**
1. `thank_you_response` - Polite responses
2. `acknowledgment` - Minimal acknowledgments
3. `check_cpu` - Infrastructure monitoring
4. `check_filesystem` - Disk checks
5. `no_tool_needed` - Avoid unnecessary tools
6. `perf_minimal_latency` - Performance baseline
7. `perf_quick_delegation` - Delegation speed

## Variant Comparison

Test prompt changes with variants:

### 1. Define Variants (in YAML)
```yaml
variants:
  baseline:
    model: gpt-4o-mini
    temperature: 0.0
    reasoning_effort: none

  improved:
    model: gpt-4o
    temperature: 0.0
    reasoning_effort: low
```

### 2. Run Both Variants
```bash
make eval EVAL_VARIANT=baseline
make eval EVAL_VARIANT=improved
```

### 3. Compare Results
```bash
make eval-compare BASELINE=eval-2025-12-30-baseline-abc123.json VARIANT=eval-2025-12-30-improved-def456.json
```

This shows:
- Pass rate delta
- Latency regression
- Token usage diff
- Per-case status changes

## Results Storage

Results are saved to `evals/results/`:

```
results/
├── eval-2025-12-30-baseline-abc123.json
├── eval-2025-12-30-improved-def456.json
└── .tmp/                                   # Per-worker temp files (auto-cleaned)
```

Each result file includes:
- Summary (pass rate, avg latency, total tokens)
- Per-case results (status, latency, assertions)
- Git commit hash
- Variant config

## Architecture

```
YAML datasets (human-editable)
    ↓
pytest plugin (load + generate tests)
    ↓
EvalRunner (in-process SupervisorService calls)
    ↓
Assertions (deterministic + LLM-graded)
    ↓
Results (JSON files + per-worker JSONL)
```

## Tips

1. **Start with hermetic mode** - Fast iteration, no API costs
2. **Tag appropriately** - Use `critical` sparingly, add `fast` for quick tests
3. **Test one thing** - Each case should test a specific behavior
4. **Use clear descriptions** - Help future maintainers understand intent
5. **Check existing tests** - Look for similar cases before adding new ones
6. **Run locally first** - `pytest evals/ -k "your_test"` before pushing

## Troubleshooting

### Test fails unexpectedly
```bash
# Run just that test with verbose output
pytest evals/ -k "test_id" -vv

# Check the full assertion output
pytest evals/ -k "test_id" -s
```

### Slow tests
```bash
# Profile which tests are slow
pytest evals/ --durations=10
```

### Marker not found
```bash
# List all markers
pytest evals/ --markers
```

### Results not merging
- Check `evals/results/.tmp/` for per-worker files
- Ensure pytest-xdist is installed: `uv sync`
- Try single-process mode: `pytest evals/` (without `-n auto`)

## References

- **Spec:** `docs/specs/eval-dataset.md`
- **Asserters:** `evals/asserters.py`
- **Runner:** `evals/runner.py`
- **Comparison:** `evals/compare.py`
