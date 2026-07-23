---
name: zerg-dev
description: Zerg local dev workflow (make dev, logs, debug, stop). Use when running or troubleshooting this repo.
---

# Zerg Dev Workflow

## Quick Start
```bash
make dev
```
This serves the local frontend at `http://localhost:47200` and proxies its API
requests to the Runtime Host already recorded in
`~/.longhouse/machine/target-url`, authenticated with the existing device token.
It should show the same account and sessions as the hosted UI. Ctrl+C stops it.

Use `make dev-demo` only when an isolated seeded local runtime is intentional.

## Common Commands
```bash
make stop      # stop local development processes
make dev-demo  # isolated local backend + seeded demo UI
```

## URL
- http://localhost:47200/timeline

## Gotchas
- Don’t assume `make dev` is already running.
- `make dev` requires an existing `longhouse auth` machine link.
- The normal local UI must never silently fall back to an empty local database.
- Use `make dev-demo` for disposable local data and `make dev` for real account data.
