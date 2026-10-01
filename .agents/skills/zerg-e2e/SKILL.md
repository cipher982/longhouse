---
name: zerg-e2e
description: Zerg E2E principles and stability guardrails. Use when fixing flaky E2E or re-enabling tests.
---

# Zerg E2E Guardrails

## Principles
- Correctness first, then speed.
- Avoid silent success: helpers must fail loudly.
- Wait on conditions, not sleeps.

## Typical Fixes
- Selector drift → add `data-testid` in React.
- Readiness waits → wait for network response, not optimistic UI.
- Shared state → isolate data per test/worker.

## Common Commands
```bash
make test-e2e-core                         # core E2E plus accessibility, retries=0
make test-e2e                              # same lane
make test-e2e-single TEST=tests/<spec>.ts
make test-e2e-single TEST=tests/<spec>.ts VERBOSE=1   # list/HTML/JUnit reporters
```

Failures land in the run's collected evidence under
`artifacts/test-isolation/<run-id>/files/` (`errors.txt`, `summary.json`); see
`zerg-testing` for the flow.
