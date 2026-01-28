---
name: zerg-dev
description: Zerg local dev workflow (make dev, logs, debug, stop). Use when running or troubleshooting this repo.
---

# Zerg Dev Workflow

## Quick Start
```bash
make dev
```
Ctrl+C stops everything cleanly.

## Common Commands
```bash
make stop      # stop all services
make logs      # tail all logs
make logs-app  # app logs only
make doctor    # quick diagnostics
make debug-trace TRACE=<uuid>
```

## Ports (dev)
- http://localhost:30080 (nginx entrypoint)
- http://localhost:47300 (backend direct)
- http://localhost:47200 (frontend direct)

## Gotchas
- Don’t assume `make dev` is already running.
- Prefer the nginx entrypoint at `http://localhost:30080`.
