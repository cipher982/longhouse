# Trigger Infrastructure Roadmap (updated 2025-05-13)

> **Key decision** – We fully control every deployment target (dev, CI, prod).
> That means we can delete compatibility shims and move fast with a simpler,
> opinionated codebase.

## Backend: Triggers

### What Exists

* **Trigger Model:**
    File: `backend/zerg/models/models.py`

        class Trigger(Base):
            __tablename__ = "triggers"
            id = Column(Integer, primary_key=True, index=True)
            agent_id = Column(Integer, ForeignKey("agents.id"), nullable=False)
            type = Column(String, default="webhook", nullable=False)
            secret = Column(String, nullable=False, unique=True, index=True)
            created_at = Column(DateTime, server_default=func.now())
            # ...model docstring: Only webhook implemented, room for email/etc
* **API Endpoints:**


* **Create Trigger**: supports `type = "webhook"` **or** `"email"` (Gmail provider today)
        See `backend/zerg/routers/triggers.py`

    * **POST `/api/triggers/{id}/events`**: Webhook endpoint, HMAC protected
* **Event bus integration**:
    When `/api/triggers/{id}/events` is called, code fires an `EventType.TRIGGER_FIRED` event, which downstream (e.g., `SchedulerService`) listens to and executes the associated agent.
* **Tests:**
    There are backend tests for "webhook trigger flow" (`tests/test_triggers.py`) confirming it will invoke agent execution.

#### Current Gaps / Not Yet Implemented

All backend plumbing for Gmail-based *email* triggers (watch registration → push webhook → history diff → `TRIGGER_FIRED`) is **implemented and fully covered by tests**.  What is still **open**:

* **Provider abstraction:** only Gmail is available – Outlook / generic IMAP remain on the roadmap.
* **Robust error handling:** add retry / exponential-backoff wrapper around Gmail HTTP requests and surface quota/auth errors via metrics.
* **Observability:** structured JSON logs, Prometheus **histograms** (latency, token counts) and alert rules are still missing.
* **Front-end polish:** real-time toast, dashboard badges and minor UX niceties are tracked in the dedicated FE section.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

## Frontend: Triggers

### What Exists

* **Trigger management UI shipped (Phases A & B)**


    * The *Agent Configuration* modal now contains a **Triggers** tab with:
        • List of existing triggers (copy-to-clipboard for webhook secret).
        • Inline *Add Trigger* wizard (webhook & email-gmail).
        • Delete trigger action.

    * Dashboard: still only shows run history, a badge with trigger count will be added in Phase E.
* **Models & API integration:**


    * `frontend/src/models.rs` defines a `Trigger` struct; `AppState.triggers` caches them by `agent_id`.

    * `api_client.rs` exposes `get_triggers`, `create_trigger`, `delete_trigger`.

    * Gmail connect button (Phase C) implemented in `auth/google_code_flow.rs`.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

## Summary Table

┌──────────┬─────────────────┬───────────────┬─────────────────┬──────────────────────┐
│ Area     │ Webhook Support │ Email Support │ UI for Triggers │ Comments             │
├──────────┼─────────────────┼───────────────┼─────────────────┼──────────────────────┤
│ Backend  │ Yes (stable)    │ ✅ Gmail (push+diff complete) │ n/a             │ Webhook & Gmail email triggers fully live │
├──────────┼─────────────────┼─────────────────┼─────────────────┼──────────────────────┤
│ Frontend │ CRUD modal live │ Connect-button live │ Partial (toast/badge pending) │ Phase D/E polish outstanding │
└──────────┴─────────────────┴───────────────┴─────────────────┴──────────────────────┘

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

## Actionable Backend TODOs for Email Triggers

### Completed backend tasks (✅)

* JSON `config` column on Trigger model
* `/api/auth/google/gmail` – auth-code → refresh-token storage
* `gmail_refresh_token` column on User
* `/api/email/webhook/google` endpoint (MVP, publishes `TRIGGER_FIRED`)
* EmailTriggerService singleton (polls, refresh-token → access-token)
* Tests: webhook + gmail flow
* Gmail Watch registration & renewal (real API with stub fallback)
* Gmail History diff + filtering; publishes `TRIGGER_FIRED` and schedules agent runs
* DELETE /triggers/{id} now stops Gmail watch (clean-up)
* Security hardening: encrypted refresh-tokens, optional Google JWT validation
* Regression tests: watch initialisation, renewal, history progression, stop-watch clean-up
* **Retry helper**: Central `zerg/utils/retry.py` decorator with expo back-off & jitter (2025-05-13)
* **Observability groundwork:** Prometheus counters + `/metrics` route implemented (2025-05-09 commit `metrics.py`).

### Outstanding backend work (🔄) – **refreshed 2025-05-18**

With the *“own the stack”* decision we removed all legacy fallbacks.  The
remaining tasks are now laser-focused:

1. **Provider abstraction**  *(core registry landed — 2025-05-11)*
   ✅ Introduce `EmailProvider` protocol and global registry in `zerg/email/providers.py`.
   ✅ `GmailProvider` now **owns the full trigger logic** – legacy helper inside `EmailTriggerService` replaced by a no-op stub (pending final removal).
   ✅ `OutlookProvider` stub raises `NotImplementedError` (placeholder for next provider).

2. **Reliability & Observability** – *DONE*
   • Gmail helpers wrapped with `@async_retry`.
   • Prometheus counters & histograms implemented; latency now recorded automatically.

3. **Security** – up to date
   • JWT validation always ON (skipped only under `TESTING=1`).
   • Fernet-at-rest for refresh tokens.

4. **Remaining tech-debt**
   • ✅ All runtime uses of `default_session_factory()` replaced (2025-05-18). Deletion of the helper itself is pending once tests migrate fully.
   • ✅ Unified logging – `zerg.utils.log.log` is now the single entry-point; legacy `logger.*` call-sites adapted (2025-05-18).
   • 🔄 Add Prometheus *histogram* helpers and corresponding unit tests.

> **Status 2025-05-11:** `Trigger.config_obj` unit-test added (`test_trigger_config.py`).

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

## Frontend TODOs (updated 2025-05-12)

Phases A, B and C are **shipped** (data-model, modal UI, Gmail OAuth).  The remaining frontend work is largely UX polish:

1. **Phase D – Real-time UX polish**
   • Show toast/banner when a run is created **via trigger** (run.source ≠ manual/schedule).
   • Increment “Triggers fired” badge on the Dashboard agent card.

2. **Phase E – Dashboard surfacing**
   • Add dedicated “Triggers” column (count) and quick-link to modal.

3. **Phase F – Tests & QA**
   • WASM tests for toast dispatcher + badge rendering.
   • Manual dark-mode & narrow-viewport QA.

4. **Disconnect flow**
   • Button that DELETEs `/api/auth/google/gmail` (endpoint TBD) and clears gmail_connected flag.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

## Kinds of Triggers Possible

* *Webhook* (already shipped)
* *Email* (**next—see above**)
* (Later: Slack, Google Calendar, File upload, Web form, etc.)

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

## Priorities for Email Trigger MVP

* **Backend**: Model/config, background service, event firing.
* **Frontend**: Add/configure via agent modal. Show status/history/log.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
## 2025-05-08 – Implementation Log / Progress

### ✅ Phase 1: Backend scaffolding (config column + stub polling service)

1. **Trigger model extended**
   • Added `config : JSON` column so arbitrary settings can be stored per-trigger (commit `models/models.py`).

2. **Pydantic schemas updated**
   • `TriggerBase`, `TriggerCreate`, `Trigger` now expose an optional `config` field (commit `schemas/schemas.py`).

3. **CRUD helper broadened**
   • `create_trigger()` accepts `config` kwarg; existing webhook tests unaffected (commit `crud/crud.py`).

4. **Trigger router tweaked**
   • Pass `trigger_in.config` through to CRUD (commit `routers/triggers.py`).

5. **EmailTriggerService stub introduced**
   • New file `services/email_trigger_service.py` with singleton `email_trigger_service`.
   • Background poller merely detects presence of `email` triggers and logs a warning.
   • Future work: connect to IMAP & publish `EventType.TRIGGER_FIRED`.

6. **Application lifecycle wired**
   • `zerg.main` now starts/stops `email_trigger_service` alongside `scheduler_service`.

7. **Database session alias**
   • Re-introduced `SessionLocal` alias for backwards compatibility (commit `database.py`).

8. **Tests**
   • Added `backend/tests/test_email_trigger_service.py` smoke-test.
   • Suite now at **110 passed / 15 skipped** (`./backend/run_backend_tests.sh` on local).
   • Coverage for email triggers remains minimal (stub detection only).

### 🔄 Smoke-Test Addition (same date)

* The new test verifies REST creation of an *email* trigger and executes the stub checker without raising.

### Next Steps

### ✅ Phase 2: Gmail OAuth & Push Skeleton (2025-06-XX)

1. **User refresh-token storage**  (`gmail_refresh_token` column).
2. **/api/auth/google/gmail** – exchanges *authorization_code* → refresh-token.
3. **EmailTriggerService** – converts refresh→access token every 60 s.
4. **/api/email/webhook/google** – receives Gmail push, publishes `TRIGGER_FIRED`, schedules agent run.
5. **Tests:** `test_gmail_webhook_trigger.py` exercises full flow (agent + trigger + webhook).

---

test `test_gmail_watch_initialisation.py` added.
### ✅ Phase 3 – Gmail Watch & History Diff  *(completed – 2025-05 → 2025-06)*

Milestone goal: end-to-end *push* flow that only fires runs for **new**
messages which pass user-defined filters.

**Implemented so far**

✅ *Watch auto-creation* (real API with stub fallback) – `initialize_gmail_trigger` persists
`history_id` & `watch_expiry` into `trigger.config`.  See
`test_gmail_watch_initialisation.py`.

✅ *Watch renewal* – `EmailTriggerService._maybe_renew_gmail_watch` now first
tries **real Gmail watch** API and falls back to the deterministic stub when
offline.  Unit-tested (`test_gmail_watch_renewal.py`).

✅ *Webhook de-duplication* – Handler tracks `last_msg_no` to avoid double
processing.  Covered by `test_gmail_webhook_trigger.py`.

✅ *History diff + filtering* – `_handle_gmail_trigger` now:
   1. Exchanges **refresh_token → access_token** via
      `gmail_api.exchange_refresh_token` (requires
      `GOOGLE_CLIENT_ID/SECRET` env vars – tests stub this).
   2. Calls `gmail_api.list_history(start_history_id)` and flattens the
      result to message-IDs.
   3. Fetches metadata for each message and evaluates
      `email_filtering.matches()` which currently supports:
      • query (substring)
      • from_contains / subject_contains
      • label_include / label_exclude
   4. Fires `TRIGGER_FIRED` events *and* schedules the agent run via
      `scheduler_service.run_agent_task`.

✅ *Cross-session commit/refresh fix* – Webhook handler now commits
`last_msg_no` *before* calling the helper and then refreshes the instance so
concurrent updates to `history_id` are merged correctly (2025-05-10).

✅ *High-level regression tests* – Added
`test_gmail_webhook_history_progress.py` ensuring
    • history_id advances to the highest value from Gmail History diff
    • a new X-Goog-Message-Number triggers another agent run.
  Suite now **114 passed** / 15 skipped.

✅ *Security hardening* –
   • `gmail_refresh_token` is stored **encrypted-at-rest** using AES-GCM (Fernet); legacy XOR fallback has been removed.
   • Optional Google JWT validation added to `/api/email/webhook/google` (enable with `EMAIL_GMAIL_JWT_VALIDATION=1`).

**Remaining technical debt (moved to *Outstanding backend work* list)**

• Robust back-off & quota handling around Gmail API requests.
• Enable JWT validation by default in prod envs (remove opt-in flag).

### ✅ Phase 3.5 – Metrics & Observability (2025-05-09)

🎯 *Goal*: first-class visibility into trigger volume and Gmail interactions.

**Delivered**

• **Prometheus integration** – Added optional dependency `prometheus-client` and
  new module `zerg.metrics` registering the following counters:
  `trigger_fired_total`, `gmail_watch_renew_total`, `gmail_api_error_total`.

• **Instrumentation** – Counters are incremented in
  `routers/triggers.py` (webhook events) and
  `services/email_trigger_service.py` (watch renewal & API error paths).

• **/metrics endpoint** – New router exposes the text exposition format; the
  route returns *501* if the library is not available so minimal CI
  environments continue to work without the extra dependency.

• **Logging** – Added `logger.info` lines around watch renewals including the
  trigger ID and processed message counts (stepping stone for structured logs).

**Next Observability steps**

1. Histogram buckets for: trigger end-to-end latency, Gmail API latency, and
   agent run duration.
2. Structured JSON logs (or OpenTelemetry spans) for every trigger event.
3. Alerting rules in staging: >1% gmail_api_error_total over 5 min = page.


**Test impact**

Because token exchange is now part of the flow, **unit tests must monkey-patch
`gmail_api.exchange_refresh_token`** or set dummy `GOOGLE_CLIENT_ID` /
`GOOGLE_CLIENT_SECRET` in the environment.  A failing test is a hint that the
patch is missing.


### 🚧 Phase 4 – Front-end CRUD / UX

– “Connect Gmail” button (in progress – `google_code_flow.rs`).
– Trigger list & wizard in Agent modal.
– Real-time toast & run history filter.

**Backend prerequisite finished** – `/api/auth/google/gmail` endpoint is now
stable; once the front-end stores the returned *success* flag the user’s
refresh-token is ready for the trigger service.

### Phase 5 – Docs & Examples

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

## Lessons learned from Phase B (UI spike)

1. **Never use native browser dialogs** (`alert / confirm / prompt`) inside the modal flow – they freeze the event loop and look off-brand.  All confirmations/input should be handled by in-app elements.
2. **Runtime DOM patching** proved valuable: users might still have an old modal HTML snippet in localStorage; injecting the new Triggers tab at runtime avoided cache-clear support tickets.
3. **Utility classes > new CSS** – by re-using existing `btn-primary`, `card`, `input-select` we shipped a visually consistent pane with zero CSS additions.
4. **Inline wizard > full modal** – the small inline “Add Trigger” card reduced context switching and performed better on narrow tablet viewports.

### 2025-05-13 – Back-end refactor retrospective

1. **Delete defunct fallbacks early** – carrying XOR crypto and std-logging
   branches bloated the codebase and complicated tests.  As soon as a hard
   requirement (Fernet, structlog) is accepted organisation-wide, purge the
   old path instead of keeping a “just in case” toggle.  Simpler code is
   easier to secure and maintain.
2. **Typed models over raw dicts** – replacing
   ``(trigger.config or {}).get("history_id")`` with a `TriggerConfig`
   dataclass removed several classes of `KeyError` / typo bugs and makes
   refactors trivial.
3. **Pick one logging format** – choosing JSON/structlog for every service
   from day-one avoids a mixed log stack (human vs. machine readable) and
   speeds up centralised monitoring roll-out.


– Update README, write `docs/triggers_email.md` example walk-through.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
---

## 2025-05-09 – **Detailed Front-end Implementation Plan**  *(tracking section)*

This section captures the concrete, incremental steps required to surface **Trigger** functionality in the Rust/WASM front-end.  Each phase should compile and be testable in isolation so that partial PRs can land without breaking `main`.

### Phase A – API & Data-model groundwork

1. `frontend/src/models.rs`
   • Add a `Trigger` struct (Serde-derived) matching the backend schema.

2. `frontend/src/network/api_client.rs`
   • New helpers: `get_triggers(agent_id)`, `create_trigger`, `delete_trigger`.

3. State layer
   • Extend `AppState` with `triggers: HashMap<u32, Vec<Trigger>>` keyed by `agent_id`.
   • Messages: `LoadTriggers(agent_id)`, `TriggersLoaded(agent_id, Vec<Trigger>)`, `TriggerCreated`, `TriggerDeleted`.

### Phase B – Agent-Modal UI

4. `components/agent_config_modal.rs`
   • Add a **“Triggers”** tab.
   • List existing triggers with copy-to-clipboard for webhook secrets.
   • “Add Trigger” wizard: choose type (`webhook`, `email:gmail`).

### Phase C – Gmail Connect flow

5. Implement / finish `google_code_flow.rs`
   • Launch Google Identity Services code-client.
   • POST auth-code → `/api/auth/google/gmail`.
   • Persist `gmail_connected` flag in `AppState`.

6. Wizard checks the flag before allowing **email** trigger creation.

### Phase D – Real-time UX polish

7. Listen for `run.created` WS messages where `trigger != manual/schedule` and toast “Trigger fired”.

8. (Optional) later subscribe to future `trigger:{id}` topics.

### Phase E – Dashboard surfacing

9. Add a “Triggers” badge / column in the Dashboard agent list (shows count).

### Phase F – Tests & QA

10. WASM tests: Trigger (de)serialisation, modal Msg dispatch.
11. Manual UX pass: dark-mode contrast, clipboard success feedback, error banners.

*Progress tracker (updated 2025-05-18):*
`[x]` **Phase A** – data-model & API helpers
`[x]` **Phase B** – modal UI (Triggers tab, list, Add-Trigger wizard, basic CRUD)
`[x]` **Phase C** – Gmail connect + email trigger enable
`[ ]` **Phase D** – real-time toast & badge polish
`[ ]` **Phase E** – Dashboard column / surfacing
`[ ]` **Phase F** – tests & QA

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
## 2025-05-12 – Front-end Phase C **completed** (Gmail OAuth live)

Commit `<hash>` introduces the UI and state plumbing for Gmail connect:

• `gmail_connected` flag added to `AppState`; toggled by new
  `Message::GmailConnected`.
• Triggers tab now shows a “Connect Gmail” button and ✓ badge once connected.
• “Email (Gmail)” option in the Add-Trigger wizard is dynamically enabled only
  when `gmail_connected == true`.
• New module `frontend/src/auth/google_code_flow.rs` holds
  `initiate_gmail_connect()` – currently a stub that immediately dispatches
  `GmailConnected` so the UI path can be demoed without real OAuth.

**Delivered today (commit `<hash>`):**

1. Real GIS *code-client* integration – popup now opens, returns `auth_code`.
2. `POST /api/auth/google/gmail` call wired; on success UI dispatches
   `GmailConnected` and enables Email-trigger option.
3. `gmail_connected` flag persisted via `/users/me` payload; survives reloads.
4. Front-end models, state, and WebSocket schema updated.
5. Cleanup: dropped legacy Python <3.10 shim in websocket manager.

Phase C checklist:

```text
[x] GIS popup + wasm_bindgen externs
[x] auth_code → backend exchange helper
[x] Persist gmail_connected across reloads
```

Next focus: **Phase D – Real-time UX polish**

1. Toast notification on `run.created` where `trigger_type != manual|schedule`.
2. Dashboard “Triggers” badge with firing counter (stretch).

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------
## 2025-05-12 – Additional Context / Clarifications  *(added after end-to-end code review)*

This section was appended after verifying the current `main` branch on
2025-05-12.  Keep it in sync with future changes.

1. **Phases A & B confirmed shipped**
   If you can open the *Agent Configuration* modal and see the “Triggers” tab
   with the “Add Trigger” inline wizard, you are on the correct build.  It is
   fully wired: selecting **Webhook** → POST `/api/triggers` → list refreshes
   with secret shown.

   • Trigger model 👉 `frontend/src/models.rs`
   • REST helpers 👉 `frontend/src/network/api_client.rs`
   • Msg / Command plumbing 👉 `frontend/src/messages.rs`, `frontend/src/update.rs`, `frontend/src/command_executors.rs`
   • Modal UI 👉 `frontend/src/components/agent_config_modal.rs`

2. **Webhook HMAC quick-start**
   A valid call to `/api/triggers/{id}/events` must include:

   ```text
   X-Zerg-Timestamp: <unix-epoch-seconds>
   X-Zerg-Signature: <hex(hmac_sha256(TRIGGER_SIGNING_SECRET, "{ts}.{raw_body}"))>
   ```

   Implementation details live in `backend/zerg/routers/triggers.py`.

3. **Critical env vars**
   • `TRIGGER_SIGNING_SECRET` – required by webhook consumers.
   • `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` – mandatory for Gmail OAuth.
   • `EMAIL_GMAIL_JWT_VALIDATION=1` enables stricter Gmail webhook checks in prod.
   • `FERNET_SECRET` – master key for encrypted refresh-token storage (required in all environments; tests inject a deterministic value).

4. **Styling rule-of-thumb**
   The Triggers tab re-uses utility classes (`btn-primary`, `card`, etc.).
   Please consult design-system owners before adding new CSS.

5. **Front-end note** – GIS code-client
   The real implementation now lives in `frontend/src/auth/google_code_flow.rs`; `/system/info` injects `google_client_id` so the Rust code can build the parameter object.

6. **Testing tip**
   When writing WASM unit tests for triggers (Phase F) enable the
   `wasm_test` feature flag which swaps real `fetch` calls for
   `gloo::net::http::FakeTransport`.




====
# Backend Refactor – 2025-05-13 Digest

This short “changelog-style” note captures the **why** and **what** of the
aggressive clean-up we just started.  It is meant for new contributors who saw
CI turn red because familiar helpers suddenly vanished.

## Why delete fallbacks now?

* **We own the fleet.**  There is no unknown third-party running the backend.
  Every engineer can upgrade Python, install `cryptography`, etc.
* **Less surface = less bugs.**  Two encryption paths or two logging
  back-ends doubles audit effort.  Removing the dead path is the cheapest
  security hardening available.
* **Simpler onboarding.**  A *single* way to encrypt, to log, to structure
  config data keeps the mental model tight for future hires.

## Decisions ratified

| Area                | Old state (up to 2025-05-12)            | New state |
|---------------------|-----------------------------------------|-----------|
| Encryption for Gmail refresh-tokens | Fernet **or** XOR fallback | Fernet only – app exits if missing |
| Logging             | std-logging **or** structlog            | structlog JSON only |
| Trigger config      | Untyped `dict` everywhere               | Typed `TriggerConfig` Pydantic model |
| Legacy agent mgr    | File kept for “compat”                  | Deleted |

## Immediate follow-ups

1. Replace remaining `logging.*` calls (<20 hits) with `log.*` from
   `zerg.utils.log`.
2. Provider abstraction for email triggers.
3. Move manual session factories to FastAPI DI (`Depends(get_db)`).

## Migration concerns?

None – the product has never been deployed to production.  We recreated the
SQLite DB from scratch during tests; no alembic migrations needed.

---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------

## Appendix – Shared Retry Helper (added 2025-05-13)

We introduced **one** decorator, `zerg/utils/retry.async_retry`, as the blessed
way to add exponential back-off.

```python
from zerg.utils.retry import async_retry, is_retryable_http_exc


@async_retry(provider="gmail", retriable=is_retryable_http_exc)
async def list_history(access_token: str, start_hid: int) -> list[dict]:
    return await _do_http("GET", url, params={...})
```

Key numbers (defaults):

* attempts   = 5 (initial + 4 retries)
* base delay = 1 s   → doubles each retry up to 30 s
* jitter     = ±25 % (prevents thundering-herd)

What you get “for free”:

* structured JSON log lines (`retry`, `retry-exhausted`)
* Prometheus counter – until the generic counter lands we bump
  `gmail_api_error_total` to surface spikes in Grafana

Guidelines
1. Decorate **provider-boundary functions only** (raw HTTP). Internal
   business logic and DB operations must fail fast.
2. If the operation is *not* idempotent, supply a custom `retriable()` that
   filters by exception type or HTTP status ≠ 409/412/4xx.
3. New providers should expose their SDK helpers *already* wrapped – call-sites
   inside the service layer stay clean.
