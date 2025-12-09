# Ops Dashboard PRD (Backend complete; Frontend next)

This document specifies an admin‑only “Ops Dashboard” and compact “Ops HUD” to give engineering a clear, always‑on operational view for daily decisions.

Audience: internal engineering

Goals

- Provide a single‑screen Ops dashboard (TV mode) and a compact HUD in the header across admin routes.
- Show live activity via a ticker, plus daily KPIs and basic timeseries/leaderboards.
- Be admin‑gated, fast to load, and easy to keep visible.
- Send budget threshold alerts and (optionally) a daily digest to Discord.

Non‑Goals

- Full observability/metrics stack (Grafana/OTel/etc.).
- Long‑term warehousing; we start with “online queries” over today’s data.
- Complex ad‑hoc slicing (Metabase could be embedded later if required).

Key KPIs

- Runs today: total (+ sparkline by hour)
- Cost today (nullable when pricing unknown)
- Budget usage: user/global gauges with thresholds (green <80%, amber 80–99.9%, red ≥100%)
- Active users (24h): created runs or messages
- Agents: total; scheduled vs idle; top 5 by cost/runs today (p95 latency)
- Latency: duration_ms p50/p95 (success runs today)
- Errors: count (finished failed) in last 1h
- Optional models breakdown (future)
- Live ticker: last N events (run started/succeeded/failed; agent created/updated; message created; budget denied)

User Stories

- As an admin, I need a “TV mode” view that continuously shows today’s KPIs and a live ticker.
- As an admin, I need a compact HUD in the header that hints at system health (runs, cost, errors, budget%).
- As an admin, I want budget alerts (80%/100%) posted to Discord with a link/context.
- As an admin, I want a daily digest summarizing yesterday and 7‑day averages (optional).

Data Sources & Contracts

REST APIs (admin‑only)

- GET `/api/ops/summary`
  - returns: `{ runs_today, cost_today_usd (float|null), budget_user {limit_cents, used_usd, percent?}, budget_global {...}, active_users_24h, agents_total, agents_scheduled, latency_ms {p50, p95}, errors_last_hour, top_agents_today: [{agent_id, name, owner_email, runs, cost_usd|null, p95_ms}] }`

- GET `/api/ops/timeseries?metric=runs_by_hour|errors_by_hour|cost_by_hour&window=today`
  - returns: `[{hour_iso: "00:00Z".."23:00Z", value: number}]`
  - cost values may be 0 when unknown; series is zero‑filled for missing hours.

- GET `/api/ops/top?kind=agents&window=today&limit=5`
  - returns: same shape as `top_agents_today` with param‑controlled window/limit.

WebSocket (admin‑only subscription)

- Topic: `ops:events`
- Envelope (shared): `{ v: 1, type: "ops_event", topic: "ops:events", ts: <ms>, data: {...} }`
- `data.type` enum:
  - `run_started|run_success|run_failed` (from RUN_CREATED/RUN_UPDATED)
  - `agent_created|agent_updated`
  - `thread_message_created`
  - `budget_denied`
- Example:
  ```json
  {
    "v": 1,
    "type": "ops_event",
    "topic": "ops:events",
    "ts": 1712345678901,
    "data": {
      "type": "run_success",
      "agent_id": 42,
      "run_id": 31337,
      "duration_ms": 1288
    }
  }
  ```

Domain Model Assumptions

- Today‑only aggregation using UTC (`func.date(ts) == today_utc`).
- Costs available when model pricing is known; otherwise null. Budgets ignore nulls.
- Agents with non‑null cron string are considered “scheduled”.

Security & Permissions

- All `/api/ops/*` endpoints require admin (`require_admin`).
- `ops:events` WS subscription requires admin; non‑admin receives 4403 close via error envelope.
- CORS remains tightened elsewhere; the ops page is internal/admin.

Configuration

- Discord alerts (instead of Slack):
  - `DISCORD_WEBHOOK_URL` (string, optional)
  - `DISCORD_ENABLE_ALERTS` (bool; default off)
  - `DISCORD_DAILY_DIGEST_CRON` (string; default `0 8 * * *`)
- Budgets:
  - `DAILY_COST_PER_USER_CENTS`, `DAILY_COST_GLOBAL_CENTS`
- Runs cap:
  - `DAILY_RUNS_PER_USER` (non‑admin)

Backend Implementation (Completed)

- Router: `backend/zerg/routers/ops.py` (admin‑gated) exposing the three endpoints above.
- Service: `backend/zerg/services/ops_service.py` (pure SQLAlchemy) with helper queries:
  - Runs today; cost today (non‑null sum); latency p50/p95; active users 24h; agents scheduled; errors last hour; top agents today; hourly series with zero‑fill.
- Event bridge: `backend/zerg/services/ops_events.py` subscribing to EventBus and broadcasting normalized ticker frames to `ops:events`.
- Budget guard hook: when budget exhausted, publish `BUDGET_DENIED` and post Discord alert (80% warn, 100% deny with de‑bounce).
- Config: added Discord envs to `Settings` and `.env.example`.

Testing Plan (Backend)

- Unit (service): summary aggregates; timeseries zero‑fill; top agents ordering/limit.
- Integration (router): admin required, structure returned, nullable cost behavior.
- WS: `ops:events` receives run + agent events; budget_denied hook emits frame; admin‑gated subscribe enforced.
- Discord: webhook calls mocked; alerts at 80/100% with de‑bounce; disabled by default.

Frontend Implementation (Next)

- Page: `/admin/ops`
  - Components:
    - KPI cards (Runs, Cost, Active Users, Errors)
    - Gauges (User/Global budgets; thresholds)
    - Sparklines (runs/errors by hour; optional cost)
    - Table: Top Agents Today
    - Live Ticker (WS `ops:events`, auto‑scroll, cap at N=200)
  - TV mode (`?tv=1`): fullscreen, large fonts, hide nav; auto‑refresh fallback (15–30s)
- Ops HUD:
  - Header widget across admin routes; polls `/api/ops/summary` every ~25s; color thresholds; click → `/admin/ops`.

Performance

- Today‑only queries on indexed timestamps; zero‑fill performed in service code.
- WS ticker uses per‑client queues; drops oldest frames beyond N=200.
- If traffic grows: add nightly rollups (e.g., `ops_daily`) and keep service pluggable.

Risks & Mitigations

- Pricing gaps ⇒ under‑reported cost/budgets; show “—” and document.
- Alert duplication ⇒ de‑bounce identical alerts for 10 minutes.
- TV mode browser tab memory ⇒ cap ticker to N=200 and throttle paint updates.
- Timezone issues ⇒ enforce UTC across queries and display; tests confirm.

Rollout Phases

1. Backend MVP (done): /api/ops/\*, ops:events, budget alerts (100% deny; warn at 80%), Discord wiring
2. Frontend: dashboard + HUD; timeseries; top agents; admin‑gated WS; optional daily digest
3. Optional: rollups, model breakdowns, spike detection/alerts

Operational Notes

- Use UTC consistently for “today”.
- Handle nullable costs (no pricing) as unknown and exclude from budget sums.
- Error classification limited to `RunStatus.failed` for now.

Appendix: Developer Runbook

- Start dev servers: `make start` (backend `:8001`, frontend `:8002`).
- Ensure admin in dev: `AUTH_DISABLED=1`, `DEV_ADMIN=1`.
- Optional Discord alerts: set `DISCORD_ENABLE_ALERTS=1` and `DISCORD_WEBHOOK_URL`.
- Test APIs:
  - `GET /api/ops/summary`
  - `GET /api/ops/timeseries?metric=runs_by_hour&window=today`
  - `GET /api/ops/top?kind=agents&window=today&limit=5`
- Test WS:
  - Connect `/api/ws`, subscribe to `ops:events`, trigger runs; watch `ops_event` frames.
