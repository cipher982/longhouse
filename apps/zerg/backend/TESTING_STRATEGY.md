# Testing Strategy: Integration Over Isolation

## Core Principle

**Mock external dependencies, run internal stack real.** You control the entire stack, so test it together where bugs actually occur.

## What to Mock (Sparingly)

### ✅ LEGITIMATE Mocking - External Dependencies

- **LLM API calls** (OpenAI, Anthropic) - expensive, rate-limited, non-deterministic
- **External HTTP services** - unreliable, slow, outside your control
- **Email services** - don't send real emails in tests
- **File system operations** (sometimes) - when testing file I/O logic specifically
- **Time operations** (sometimes) - when testing time-sensitive logic

### ❌ DO NOT Mock - Internal Stack

- **Database operations** - use test database, this catches schema issues
- **Internal service calls** - FicheRunner, ThreadService, WorkflowEngine
- **Message serialization/deserialization** - core business logic
- **WebSocket connections** (in integration tests)
- **ORM model operations** - catches field name mismatches
- **Internal HTTP endpoints** - use TestClient for real request/response cycle

## Integration Test Examples

### ✅ PROPER Integration Test

```python
# tests/test_basic_fiche_workflow_e2e.py - Tests exact "add fiche, press run" scenario
with patch('zerg.services.oikos_react_engine.run_oikos_loop') as mock_loop:
    mock_loop.return_value = OikosResult(messages=[...], usage={}, interrupted=False)

    # Everything else runs REAL:
    execution_id = await workflow_engine.execute_workflow(workflow.id)

    # Tests real ThreadMessage serialization, real datetime operations,
    # real database transactions - would have caught both bugs!
```

### ❌ Over-Mocked Test (Problem)

```python
# tests/test_conditional_workflows.py - Over-mocked version
with patch("zerg.services.node_executors.FicheRunner") as mock_fiche_runner:
    mock_fiche_runner.return_value.run_thread = lambda: [{"role": "assistant"}]  # Fake!

    # Skips real ThreadMessage creation, real serialization, real datetime handling
    # Tests passed but production failed because they tested fake scenarios
```

## Test Coverage Status

### ✅ NEW Integration Tests (100% Passing)

1. **`test_basic_fiche_workflow_e2e.py`** - Basic "add fiche, press run" workflow
2. **`test_conditional_workflows_integration.py`** - Real conditional logic with minimal mocking
3. **Both test real ThreadMessage serialization** - catches field name bugs
4. **Both test real datetime operations** - catches timezone subtraction bugs

### ✅ Fixed Original Tests

- **`test_conditional_workflows.py`** - Updated mocks to use proper ThreadMessage objects
- All original tests still pass with better mock data structures

## Bug Prevention

The two critical bugs you encountered would be **impossible** with proper integration tests:

1. **`ThreadMessage.created_at` bug** - Real ThreadMessage objects have `timestamp` field
2. **Datetime subtraction error** - Real workflow execution uses consistent timezone handling

**Key insight**: Over-mocked tests gave false confidence by testing fake scenarios while real integration points were bypassed.

## Usage (2026-01-31 Lite Pivot)

We now maintain **two** backend suites:
- **Lite suite** (SQLite, fast) → default for OSS refactor work
- **Legacy suite** (Postgres, full) → regression coverage for enterprise paths

The lite suite is the default `make test` path. The legacy suite is opt-in.

### Commands

```bash
# Default (SQLite-lite, fast)
make test

# Legacy full suite (Postgres)
make test-legacy

# Backend-only lite tests (supports passing extra pytest args)
cd apps/zerg/backend && ./run_backend_tests_lite.sh -k sqlite

# Backend-only legacy tests (supports passing extra pytest args)
cd apps/zerg/backend && ./run_backend_tests.sh -k oikos_tools
```
