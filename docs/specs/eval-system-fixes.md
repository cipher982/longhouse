# Eval System Fixes Spec

**Status:** In Progress
**Created:** 2025-12-30
**Protocol:** SDP-1

## Executive Summary

Post-implementation review of the eval dataset system revealed several issues that need addressing. This spec covers fixes for broken metrics, outdated documentation, schema improvements, and test quality enhancements.

## Decision Log

### Decision: Keep hermetic mode focused on infrastructure testing
**Context:** Many hermetic tests don't actually test agent behavior
**Choice:** Accept this limitation, document it clearly, don't try to make hermetic tests test LLM behavior
**Rationale:** Hermetic mode's value is fast CI/crash detection. Behavior testing belongs in live mode.
**Revisit if:** We get deterministic LLM behavior via seeded sampling

### Decision: Don't add negative tests in this PR
**Context:** Missing tests for rejection/error cases
**Choice:** Document as future work, don't expand scope
**Rationale:** Current fixes are already substantial; negative tests need design thought
**Revisit if:** We see actual production issues from missing negative coverage

### Decision: Keep EvalAssertion schema as-is
**Context:** `value` field is overloaded across assertion types
**Choice:** Document the overloading, don't refactor to discriminated unions
**Rationale:** Current schema works, refactor would touch many files for marginal benefit
**Revisit if:** We add more assertion types and the mapping becomes unmaintainable

## Issues to Fix

| # | Issue | Severity | Fix |
|---|-------|----------|-----|
| 1 | `basic.yml` description says "18 test cases" but has 53 | Low | Update description |
| 2 | Token tracking shows 0 for all tests | Medium | Stub doesn't track tokens; document limitation |
| 3 | README doesn't clarify hermetic vs live mode value | Medium | Add "What Each Mode Tests" section |
| 4 | Critical tests don't test anything meaningful in hermetic | Medium | Document; recommend live mode for real gates |
| 5 | Variants meaningless in hermetic mode | Low | Document limitation |
| 6 | Live mode only has 2 tests | Medium | Add more live mode test cases |

## Implementation Phases

### Phase 1: Documentation & YAML Fixes

**Scope:** Fix description, improve README clarity

**Changes:**
1. Update `basic.yml` description from "18 test cases" to "53 test cases"
2. Add "What Each Mode Tests" section to README explaining hermetic vs live value
3. Add "Limitations" section documenting token tracking, variant testing
4. Update critical tests documentation to recommend live mode for real deployment gates

**Acceptance Criteria:**
- [ ] `basic.yml` description accurate
- [ ] README has "What Each Mode Tests" section
- [ ] README has "Known Limitations" section
- [ ] Existing tests still pass

**Test Command:** `cd apps/zerg/backend && uv run pytest evals/ -v -n auto --timeout=60`

### Phase 2: Expand Live Mode Tests

**Scope:** Add meaningful live mode tests that actually validate agent behavior

**Changes:**
1. Add 5+ new live mode tests covering:
   - Tool selection quality
   - Worker delegation decisions
   - Knowledge base usage
   - Multi-turn context retention
   - Edge case handling
2. Update live.yml with proper rubrics

**Acceptance Criteria:**
- [ ] live.yml has 7+ test cases (up from 2)
- [ ] Tests cover different agent behaviors
- [ ] Rubrics are specific and measurable
- [ ] Tests skip correctly in hermetic mode

**Test Command:** `cd apps/zerg/backend && uv run pytest evals/ -v --timeout=60` (verify skips)

### Phase 3: Final Validation

**Scope:** Run full test suite, verify no regressions

**Acceptance Criteria:**
- [ ] `make eval` passes (53 hermetic tests)
- [ ] `make eval-critical` passes (7 critical tests)
- [ ] Live tests show as "skipped" in hermetic mode
- [ ] No new warnings or errors

**Test Commands:**
```bash
make eval
make eval-critical
```

## Files to Modify

| File | Changes |
|------|---------|
| `apps/zerg/backend/evals/datasets/basic.yml` | Fix description |
| `apps/zerg/backend/evals/datasets/live.yml` | Add test cases |
| `apps/zerg/backend/evals/README.md` | Add sections on mode differences, limitations |

## Out of Scope

- Refactoring EvalAssertion schema (Decision: keep as-is)
- Adding negative tests (future work)
- Fixing stub to emit fake tokens (complex, low value)
- Making hermetic tests actually test behavior (not possible without real LLM)
