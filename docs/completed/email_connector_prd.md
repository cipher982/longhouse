# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---

# PRD: Email Connector-First Integration (Gmail) – Backend + Frontend Alignment

## Summary

We are refactoring email integrations to a connector-first model and moving inbound Gmail processing from poller-driven to webhook-first (ultimately Pub/Sub) with providers encapsulated in `zerg/email/providers.py`. This PRD documents context, work completed, lessons learned, security posture, and the prioritized plan to productionize the flow (including Pub/Sub correctness) and align the frontend.

## Goals

- Single source of truth for provider credentials and metadata via Connectors.
- Decoupled provider behavior (Gmail, later Outlook/IMAP/SMTP) behind an EmailProvider abstraction.
- Webhook-first flow for Gmail to reduce latency and complexity.
- Frontend to create connector-aware triggers: `{ "type": "email", "config": { "connector_id": <id> } }`.
- Secure, observable, and extensible architecture suitable for additional providers.

## Non-goals

- Complete Pub/Sub infrastructure provisioning (topic/subscription) in this iteration.
- Full connectors UI/management beyond a minimal list/delete API.

## Background / Context

- Previously, Gmail refresh tokens lived on the User model and triggers embedded provider details.
- Processing logic was coupled in an EmailTriggerService with mixed responsibilities (polling, matching, scheduling).
- Gmail push was assumed to use Drive-style HTTPS headers, but Gmail uses Cloud Pub/Sub for push.

## Current State (after this work)

- Connector model (`backend/zerg/models/models.py`):
  - Columns: `id`, `owner_id`, `type`, `provider`, `config` (encrypted secrets), `created_at`, `updated_at`.
  - Constraints: Unique `(owner_id, type, provider)`.
- CRUD helpers for connectors in `backend/zerg/crud/crud.py`.
- DB registration in `backend/zerg/database.py` so `create_all()` includes connectors.
- Gmail connect flow `POST /api/auth/google/gmail`:
  - Exchanges auth code, encrypts and stores refresh token in a connector (upsert with uniqueness handling), returns `connector_id`.
  - Derives callback URL from `APP_PUBLIC_URL` (or validates strictly in non-test env).
- Gmail webhook `POST /api/email/webhook/google`:
  - Connector-centric, persists dedupe (`last_msg_no`), then schedules background processing of the connector and returns 202.
- Provider abstraction:
  - `GmailProvider.process_connector(connector_id)` refreshes token, lists history, prefetches metadata, matches triggers referencing the connector, publishes `TRIGGER_FIRED`, schedules agent, and advances `history_id`.
- Frontend alignment (minimal):
  - Trigger creation uses `type: "email"` and includes `config.connector_id`.
  - Gmail connect parses `connector_id` response and stores it; state has `gmail_connector_id`.
- Minimal connectors API:
  - `GET /api/connectors` (current user only; secret fields redacted), `DELETE /api/connectors/{id}`.

## Lessons Learned

- Gmail push notifications use Cloud Pub/Sub, not direct HTTPS headers like Drive/Calendar; production flow must switch to Pub/Sub push + OIDC validation.
- Keeping webhooks light (return 202, process async) significantly improves responsiveness and resilience to upstream timeouts.
- Uniqueness constraints at the DB level prevent duplicate connectors; upserts should be robust against races.
- Computing callback URLs server-side (via `APP_PUBLIC_URL`) prevents SSRF and misconfiguration.
- Redacting secrets in API responses is essential once connectors are exposed to the UI.

## What’s Done

- Connector-first backend refactor and provider encapsulation.
- Async Gmail webhook (202 + background task) with dedupe persisted prior to scheduling.
- Connector schema hardening: `(owner_id, type, provider)` uniqueness and `updated_at`.
- Gmail connect endpoint callback hardening: derive from `APP_PUBLIC_URL` and validate.
- Minimal connectors API (list/delete; redaction).
- Frontend: connector-aware email triggers, storing `gmail_connector_id` and using it in trigger creation; parsing `connector_id` from connect.
- Trigger type allowlist validation (`webhook`, `email`).

Status tracker:

- Webhook lightening: DONE
- Connector uniqueness + updated_at: DONE
- Connect endpoint hardening: DONE
- Frontend trigger payloads + connector_id handling: DONE
- Minimal connectors API: IN PROGRESS (GET/DELETE done; POST/PATCH later)
- Pub/Sub-correct Gmail flow: TODO
- Watch renewal at connector level: TODO
- Observability metrics/gauges: TODO
- Test hygiene (scoping raise_server_exceptions=False; connectors API tests): TODO
- Clean legacy EmailTriggerService (delete or clearly fallback): TODO
- Docs and configs updates (.env.example, retry semantics): TODO

## Remaining Work (Prioritized)

1. Pub/Sub-correct Gmail push integration (High)
   - Add `/api/email/webhook/google/pubsub` route to receive Pub/Sub push.
   - Validate OIDC tokens (issuer, audience), parse payload, map to connector (store `emailAddress` on connector during connect/init via `users.getProfile`).
   - Update `gmail_api.start_watch` to accept a Pub/Sub topic and watch init; persist `emailAddress`.
2. Observability & metrics (Med)
   - Prometheus counters for webhook dedupe, processing outcomes; gauge for `history_id` per connector; logs include friendly identifiers (owner_id, provider).
3. Watch renewal (Med)
   - Connector-level renewal loop aligned with Gmail’s 7-day expiration; persist renewal metadata.
4. Frontend hydration & connectors UI (Med)
   - On login/startup, hydrate connectors list to set `gmail_connector_id` automatically; add a simple Connectors view or picker when multiple connectors exist.
5. Test hygiene & coverage (Med)
   - Scope `raise_server_exceptions=False` only where needed; add connectors API tests (ownership, redaction).
6. Clean up legacy (Low)
   - Remove old EmailTriggerService poller or mark as disabled fallback.
7. Documentation & env examples (Low)
   - Update `.env.example` (FERNET_SECRET, APP_PUBLIC_URL, GOOGLE_CLIENT_ID/SECRET, Pub/Sub config) and document webhook latency/async semantics and dedupe behavior.

## Security Considerations

- Secrets at rest encrypted via Fernet (`FERNET_SECRET` required in prod).
- Server-derived callback URLs; strict validation when not deriving.
- Pub/Sub push must verify OIDC tokens per Google guidance.
- Webhook dedupe currently header-based; Pub/Sub production flow should rely on message data/attributes instead.
- Connector APIs enforce owner scoping and redact secrets by default.

## API Changes (Summary)

- `POST /api/auth/google/gmail` → returns `{ status, connector_id }`; computes/validates callback.
- `POST /api/email/webhook/google` → 202 Accepted immediately; async processing.
- `GET /api/connectors` → list current user’s connectors (redacted config).
- `DELETE /api/connectors/{id}` → delete if owned.
- `GET /api/system/info` → now includes `app_public_url`.

## Frontend Changes (Summary)

- Gmail connect flow parses `connector_id` and updates state (`gmail_connector_id`).
- Trigger creation for email uses `{ "type": "email", "config": { "connector_id": <id> } }`.
- State: `gmail_connected: bool`, `gmail_connector_id: Option<u32>`; UI enables email trigger when connected and id is known.
- Future: hydrate connectors on startup; connectors picker when multiple exist.

## Migration Plan

- If any deploy uses `User.gmail_refresh_token`, perform one-time migration:
  - Create Gmail connector per user and move encrypted refresh token to connector config; clear or deprecate the user column.
- Legacy triggers of shape `type: "email:gmail"` should be rejected (breaking change is acceptable pre-GA); optionally add a short-lived shim with warnings.

## Acceptance Criteria

- Backend webhook returns 202 quickly; processing happens in background.
- Creating an email trigger via UI with a connected Gmail account produces a trigger wired to the correct connector and fires on new emails (test stubs acceptable).
- Connector uniqueness enforced; duplicate connect attempts reuse existing connector.
- Connectors API returns redacted configs and only the current user’s connectors.
- Security: callback URL clamped; secrets encrypted; no secret leakage in responses.

## References

- Gmail Push Notifications (Pub/Sub): https://developers.google.com/gmail/api/guides/push
- Gmail History API: https://developers.google.com/gmail/api/reference/rest/v1/users.history/list
- Gmail Messages API: https://developers.google.com/gmail/api/reference/rest/v1/users.messages/get
- Pub/Sub Push Auth (OIDC): https://cloud.google.com/pubsub/docs/push
- Google Identity Services Code Flow: https://developers.google.com/identity/oauth2/web/guides/use-code-model
