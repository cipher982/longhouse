# Ops Dashboard – Progress & Next Steps

Scope: admin‑only Ops dashboard (backend done), frontend dashboard + HUD (next).

Backend (Completed)

- [x] API: `GET /api/ops/summary`
- [x] API: `GET /api/ops/timeseries?metric=...&window=today`
- [x] API: `GET /api/ops/top?kind=agents&window=today&limit=5`
- [x] Service: summary/timeseries/top (zero‑fill, percentile calc, nullable cost)
- [x] WS: `ops:events` ticker (run*\*, agent*\*, thread_message_created, budget_denied)
- [x] Admin gating: ops APIs + ops:events subscription
- [x] Budget guard: 80% warn, 100% deny (Discord alerts; de‑bounced)
- [x] Config: `DISCORD_WEBHOOK_URL`, `DISCORD_ENABLE_ALERTS`, `DISCORD_DAILY_DIGEST_CRON`
- [x] Tests: service unit, router integration, WS ticker mapping, admin gating, budget alerts
- [x] AsyncAPI: formalized `ops_event` (typed `OpsEventData`); regenerated WS types/handlers (backend/frontend) and switched ops bridge to typed emitter

How to Run (Dev)

- `make start` (backend `:8001`, frontend `:8002`)
- Ensure admin in dev: `AUTH_DISABLED=1`, `DEV_ADMIN=1`
- Optional alerts: set `DISCORD_ENABLE_ALERTS=1` and `DISCORD_WEBHOOK_URL`

Lessons Learned

- Keep WS frames in a single envelope to simplify clients.
- Use UTC for “today” and percentiles computed in Python for SQLite compatibility.
- Treat unknown costs as null (display “—”) and exclude from budget sums.
- Debounce alerts to prevent Discord spam.
- Admin gating at both REST and WS subscription prevents accidental exposure.

Frontend (Next)

- [ ] Page `/admin/ops`: KPI cards, budget gauges, sparklines, top agents, live ticker (cap N=200)
- [ ] TV mode: `?tv=1` fullscreen, large fonts, hide nav; 15s auto‑refresh fallback
- [ ] Ops HUD: compact metrics across admin routes; poll summary every ~25s; thresholds (runs, cost, errors, budget%)
- [ ] WS integration: subscribe to `ops:events`; append frames; color‑coded types
- [ ] Error/loading states; skeletons; retry logic
- [ ] Tests: component render, admin gating, ticker behavior, zero‑fill charts
- [ ] Optional: Discord daily digest job wiring (CRON + summary formatter)

Reference

- PRD: `docs/OPS_DASHBOARD_PRD.md`
