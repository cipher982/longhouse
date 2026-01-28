---
name: zerg-testing
description: Zerg testing workflow (unit + E2E). Use when running or debugging tests.
---

# Zerg Testing

## Rules
- Always use Make targets. Never run pytest/bun/playwright directly.

## Core Commands
```bash
make test                # unit tests
make test-e2e-core       # core E2E (must pass 100%)
make test-e2e            # full E2E (retries ok)
make test-all            # unit + full E2E
make test-e2e-single TEST=tests/<spec>.ts
make test-e2e-errors     # show last E2E errors
make test-e2e-verbose    # full output for debugging
```

## Debugging Flow
1) `make test-e2e-errors`
2) `make test-e2e-single TEST=tests/<spec>.ts`
3) `make test-e2e-verbose`
