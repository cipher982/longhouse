# ✅ COMPLETED / HISTORICAL REFERENCE ONLY

> **Note:** This feature has been implemented. Implementation details may have evolved since this document was written.
> For current documentation, see the root `docs/` directory.

---

# Remaining Tool Features — Pre-Beta Slim Spec

**Status**: Beta-ready implementation spec (slimmed to match current codebase + pre-launch needs)
**Created**: 2025-12-18
**Updated**: 2025-12-18

## What “Remaining” Means Here

This doc is about **missing tool capabilities** (things agents can do via tools) and the minimum trigger webhook needed for beta. It is intentionally biased toward: **ship, don’t overbuild**.

## Reality Check (Already In The Repo)

### Built-in tools already implemented

- `web_search` (`apps/zerg/backend/zerg/tools/builtin/web_search.py`)
- `web_fetch` (`apps/zerg/backend/zerg/tools/builtin/web_fetch.py`)
- `contact_user` (`apps/zerg/backend/zerg/tools/builtin/contact_user.py`)

### “Webhook” already exists in 3 different senses

1. **Incoming agent triggers**

- Backend: `apps/zerg/backend/zerg/routers/triggers.py`
- Endpoints exist today:
  - `POST /api/triggers/` (create)
  - `GET /api/triggers/` (list)
  - `DELETE /api/triggers/{trigger_id}` (delete)
  - `POST /api/triggers/{trigger_id}/events` (fire)
- Current mismatch:
  - The router is mounted behind `get_current_user`, so `/events` is **not a true external webhook** in production.
  - Each Trigger row has a per-trigger `secret` (`apps/zerg/backend/zerg/models/models.py`), but `/events` currently validates an HMAC using the global `TRIGGER_SIGNING_SECRET` (`apps/zerg/backend/zerg/constants.py`), not the trigger secret.

2. **Incoming Gmail webhooks**

- Backend: `apps/zerg/backend/zerg/routers/email_webhooks.py` and `apps/zerg/backend/zerg/routers/email_webhooks_pubsub.py`

3. **Outgoing “webhooks” (Slack/Discord notifications)**

- Tools: `send_slack_webhook`, `send_discord_webhook` (`apps/zerg/backend/zerg/tools/builtin/`)
- Credentials: Connectors + UI (`apps/zerg/frontend-web/src/pages/IntegrationsPage.tsx`)

## Beta Scope (Only These 3)

1. **User Task Management** (tool-only CRUD)
2. **Agent Persistent Memory** (KV-only; no SQL interface)
3. **Public Trigger Webhook (B-min easy mode)** (make existing trigger `/events` actually usable externally)

Everything else is deferred (quota tiers, SQL sandboxing, audit tables, replay tables, workflow-level webhook triggers).

---

## 1) User Task Management (Tool-only MVP)

### Non-goals (beta)

- No UI
- No recurrence
- No bulk update
- No search/tags/rate limits until you have real users demanding it

### Minimal data model

```sql
CREATE TABLE user_tasks (
  id SERIAL PRIMARY KEY,
  user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  title TEXT NOT NULL,
  notes TEXT,
  status TEXT NOT NULL DEFAULT 'pending', -- pending|done|cancelled
  due_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX user_tasks_user_id_idx ON user_tasks(user_id);
CREATE INDEX user_tasks_user_id_status_idx ON user_tasks(user_id, status);
```

### Tools (MVP)

- `task_create(title, notes?, due_at?)`
- `task_list(status?, limit=50, offset=0)`
- `task_update(task_id, title?, notes?, status?, due_at?)`
- `task_delete(task_id)`

### Security rule

Every operation must scope by `user_id == current_user.id`.

### Suggested repo locations

- Model/migration: `apps/zerg/backend/alembic/versions/`
- Tool module: `apps/zerg/backend/zerg/tools/builtin/task_tools.py`
- Tests: `apps/zerg/backend/tests/`

---

## 2) Agent Persistent Memory (KV-only MVP)

### Decision (beta)

KV-only in Postgres. No SQL execution. No per-user schemas. No allowlist/blocklist SQL “sandbox”.

### Minimal data model

```sql
CREATE TABLE agent_memory_kv (
  user_id INT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value JSONB NOT NULL,
  tags TEXT[] DEFAULT '{}',
  expires_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (user_id, key)
);
CREATE INDEX agent_memory_kv_tags_idx ON agent_memory_kv USING GIN(tags);
CREATE INDEX agent_memory_kv_expires_idx ON agent_memory_kv(expires_at) WHERE expires_at IS NOT NULL;
```

### Tools (MVP)

- `agent_memory_set(key, value, tags?, expires_at?)`
- `agent_memory_get(key?, tags?, limit=100)`
- `agent_memory_delete(key?, tags?)`
- `agent_memory_export()` (size-limited)

### Security rule

Every operation must scope by `user_id == current_user.id`.

### Suggested repo locations

- Tool module: `apps/zerg/backend/zerg/tools/builtin/agent_memory_tools.py`
- Tests: `apps/zerg/backend/tests/`

---

## 3) Public Trigger Webhook — B-min (“easy mode”)

### Decision (beta)

Make **only** the existing trigger fire endpoint public:

- `POST /api/triggers/{trigger_id}/events` becomes callable without Swarmlet auth
- Authentication becomes: `Authorization: Bearer <trigger.secret>`

Keep trigger management endpoints auth-required:

- `POST /api/triggers/`, `GET /api/triggers/`, `DELETE /api/triggers/{trigger_id}` remain behind `get_current_user`

### Request

- Method: `POST`
- Path: `/api/triggers/{trigger_id}/events`
- Headers:
  - `Authorization: Bearer <trigger.secret>` (required)
  - `Content-Type: application/json` (recommended)
- Body:
  - arbitrary JSON object (or empty)

### Response

To avoid leaking whether a trigger exists, return **404 for both “bad token” and “unknown trigger”**.

- `202 Accepted`: triggered successfully
  - Body: `{ "status": "accepted" }`
- `404 Not Found`: missing/invalid token OR trigger not found/inactive
  - Body: `{ "detail": "Not found" }`
- `413 Payload Too Large`: request body too large
- `429 Too Many Requests`: rate limited (recommended even if naive/in-memory)

### Limits (beta)

- Max body size: `256 KiB` (reject before doing any expensive work)
- Rate limiting: per `trigger_id` (and optionally per IP)
  - Start simple; correctness > sophistication pre-beta.

### Processing behavior

- Validate token, validate trigger exists, then:
  - publish internal event (`TRIGGER_FIRED`)
  - start the agent run (current behavior is acceptable)
- Return `202` fast. Do not block on long agent execution.

### Example (curl)

```bash
curl -X POST "https://api.swarmlet.com/api/triggers/123/events" \
  -H "Authorization: Bearer <trigger_secret>" \
  -H "Content-Type: application/json" \
  -d '{"event":"deploy_done","env":"prod"}'
```

### Explicit non-goals (beta)

- No Stripe-style HMAC signature headers
- No DB-backed replay protection tables
- No audit log tables
- No “unguessable public_id” migration (defer unless you see probing noise)
- No workflow-level webhook triggers (this is agent-trigger-based)

### Required code changes (to implement B-min)

- `apps/zerg/backend/zerg/routers/triggers.py`: make `/events` public, validate bearer token against `Trigger.secret`
- `apps/zerg/backend/tests/test_triggers.py`: update test to pass bearer token instead of HMAC headers
- (Optional after switching) `TRIGGER_SIGNING_SECRET` becomes unnecessary for trigger firing; decide whether to keep it for other uses or remove the requirement in production.

---

## Deferred Until You Have Users Asking For It

- SQL-backed “agent memory” with sandboxing / per-user schemas
- Task rate limits, bulk operations, tags/search
- Webhook audit logs, replay tables, secret rotation UX, multi-secret overlap, dedicated `public_id` per webhook
