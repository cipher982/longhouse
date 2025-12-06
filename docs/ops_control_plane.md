# Unified Ops Control Plane Proposal

## 1. Problem & Opportunity

- **Current flow**: Agents (human or AI) SSH into prod, run docker-compose commands, and manually scrape logs from disparate sources (backend, frontend, deploy scripts, browser console).
- **Pain**: Hard to automate, no single source of truth, frontend diagnostics require copy/paste, and every helper agent needs shell access.
- **Goal**: Deliver a robust, flexible, API-first control plane that consolidates operational data into structured payloads that are easy to query (JSON + `jq`) or view as human-readable text.

## 2. Design Principles

1. **Single surface** – admin-only `/api/ops/*` endpoints + WebSocket ticker provide all telemetry, replacing ad hoc SSH docs.
2. **Composable JSON** – responses remain machine-friendly; `opsctl` (a thin CLI) can pipe into `jq` or `less`.
3. **Frontend parity** – browser errors and build hashes appear alongside backend stats.
4. **No-regrets rollout** – builds on existing ops APIs (`/api/ops/summary`, `/api/ops/timeseries`, `ops:events`) so we can ship iteratively.

## 3. High-Level Architecture

```
┌──────────────────────────────┐
│ Existing Data Sources        │
│ • Ops summary/timeseries     │
│ • Docker/container health    │
│ • Git + deploy metadata      │
│ • Backend/Frontend logs      │
│ • Frontend beacon telemetry  │
└──────────────┬──────────────┘
               │
       ┌───────▼────────┐
       │ Ops Control API│   (FastAPI router, admin-gated)
       └───────┬────────┘
               │
      ┌────────▼──────────┐
      │ Clients            │
      │ • Laptop agents    │
      │ • opsctl CLI       │
      │ • Jarvis/Discord   │
      └────────────────────┘
```

## 4. API Surface

| Endpoint                                               | Purpose                                                                                                                 | Notes                                                                                                          |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `GET /api/ops/context`                                 | Unified JSON snapshot (deploy info, migrations, Docker health, budgets from `/api/ops/summary`, latency, active alerts) | Merge existing summary + new sections: `deploy`, `docker`, `db`, `frontend`, `alerts`.                         |
| `GET /api/ops/logs?service=<name>&tail=200&format=json | plain`                                                                                                                  | Tail logs for backend/frontend/nginx/postgres                                                                  | Wraps `docker compose logs`; normalize timestamps. JSON mode emits `[{ts, level, message}]`, plain mode pipes through untouched text. |
| `GET /api/ops/support-bundle`                          | Download gzipped JSON containing `context`, rolling logs, recent deploy diffs                                           | Replaces current “SSH cheat sheet”; easy to archive or attach to tickets.                                      |
| `POST /api/ops/frontend-log`                           | Browser beacon for console errors, build hash, route, stack trace                                                       | Stores in Redis/Postgres ring buffer; backend also mirrors last-known frontend state into `context.ui_status`. |
| `GET /api/ops/frontend-logs?since=5m`                  | Fetch recent UI issues without visiting the site                                                                        | Output is JSON so agents can triage automatically.                                                             |
| `WS ops:events` (existing)                             | Live ticker for run/agent/budget events                                                                                 | Continue exposing for real-time dashboards; agents can subscribe for proactive detection.                      |

## 5. Laptop Agent Workflow (opsctl)

1. Authenticate once using the same admin session/JWT flow already used for ops endpoints.
2. Provide simple verbs:
   - `opsctl summary` → `GET /api/ops/context`
   - `opsctl logs backend --plain` → text for quick paging
   - `opsctl logs frontend | jq '.entries[] | {ts,message}'`
   - `opsctl bundle > bundle.json.gz`
3. Keep implementation minimal (bash + curl + jq) so any local agent can call it reliably.

## 6. Frontend Telemetry Capture

- Inject a lightweight reporter in the SPA that:
  - Hooks `console.error`, global `window.onerror`, and `unhandledrejection`.
  - Packages `{build_hash, route, message, stack, browser, ws_state}`.
  - Sends via `navigator.sendBeacon('/api/ops/frontend-log', payload)` to avoid blocking UX.
- Backend stores recent events (e.g., last 500 per route) and exposes them via the new APIs. This eliminates manual screenshot/log copy for agents debugging UI regressions.

## 7. Reliability, Security & Value

- **Auth**: Reuse admin gating already present on ops endpoints and SSE streams. Consider short-lived service accounts for laptop agents.
- **Backpressure**: Rate-limit log endpoints, cap support bundle size, and use ring buffers for frontend events.
- **Auditability**: All diagnostics go through the API, so access is logged (unlike SSH).
- **Incremental Value**:
  1. Agents no longer need root access just to tail logs.
  2. Frontend issues are first-class citizens.
  3. Support bundles + JSON snapshots allow automated diffing between deploys.

## 8. Implementation Plan

1. **Phase 1** – Ship `/api/ops/context` by composing existing `/api/ops/summary` data with release metadata (git SHA, docker image tags, migration status).
2. **Phase 2** – Add `/api/ops/logs` and the `opsctl` CLI wrapper; document service names + retention rules.
3. **Phase 3** – Implement frontend beacon + `/api/ops/frontend-log(s)` endpoints.
4. **Phase 4** – Generate full support bundles and optional diffing helpers; integrate with Jarvis or Discord alerts for proactive agents.
5. **Phase 5** – Explore synthetic checks (e.g., automated frontend heartbeat) feeding into the same API.

## 9. Comparison vs. Manual SSH Docs

- **Before**: share text file with SSH instructions → run commands manually → copy logs into laptop agent.
- **After**: run `opsctl summary` / `opsctl logs frontend`; everything is authenticated, structured, and automatable.
- **Net**: Faster triage, safer access model, easier for AI agents to analyze and propose fixes autonomously.
