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
