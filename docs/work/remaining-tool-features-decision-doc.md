# Remaining Tool Features - Implementation Spec

**Status**: Final spec - ready for implementation
**Created**: 2025-12-18
**Updated**: 2025-12-18 (incorporated reviewer feedback)
**Context**: Competitive analysis identified gaps; web_search, web_fetch, and contact_user are complete. Three features remain.

---

## Executive Summary

| Feature                 | Priority | Complexity  | Decision                        |
| ----------------------- | -------- | ----------- | ------------------------------- |
| User Task Management    | High     | Low         | Tool-only MVP with audit trail  |
| Agent Persistent Memory | High     | Medium-High | Hybrid (schemas + KV)           |
| Webhook Triggers        | Medium   | Medium      | HMAC default + timestamped sigs |

**Implementation Order**: Tasks → Memory → Webhooks (quick win first, then highest impact)

---

## Feature 1: User Task Management

### Decision Summary

- **Scope**: Tool-only MVP (no UI initially)
- **Sharing**: No cross-user sharing
- **Notifications**: No push notifications; urgent tasks surface in next Jarvis session
- **Recurrence**: No
- **External integrations**: Future consideration

### Threat Model

| Threat               | Mitigation                                                        |
| -------------------- | ----------------------------------------------------------------- |
| Silent state changes | Audit trail with `source`, `origin_run_id`, `updated_by_agent_id` |
| Task spam            | Rate limiting on task creation (100/hour/user)                    |
| Data leakage         | Tasks scoped to user_id, validated on every operation             |

### Data Model

```python
class UserTask(Base):
    __tablename__ = "user_tasks"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)

    # Core fields
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    priority: Mapped[str] = mapped_column(String(20), default="normal")
    due_date: Mapped[datetime | None]
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=[])

    # Audit trail (critical for "who changed my tasks?" visibility)
    source: Mapped[str] = mapped_column(String(20))  # "agent" | "user" | "api"
    created_by_agent_id: Mapped[UUID | None] = mapped_column(ForeignKey("agents.id"))
    origin_run_id: Mapped[UUID | None]  # Which run created this
    updated_by_agent_id: Mapped[UUID | None]  # Last modifier

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime | None] = mapped_column(onupdate=func.now())
    completed_at: Mapped[datetime | None]

# Constraints
# - status IN ('pending', 'in_progress', 'completed', 'cancelled')
# - priority IN ('low', 'normal', 'high', 'urgent')
```

### Tools

```python
# Location: apps/zerg/backend/zerg/tools/builtin/task_tools.py

def task_list(
    status: str | None = None,     # Filter by status
    tags: list[str] | None = None, # Filter by tags (AND logic)
    limit: int = 50,               # Max results (1-100)
    offset: int = 0,               # Pagination
) -> dict:
    """List tasks for the current user."""

def task_search(
    query: str,                    # Search in title/description
    status: str | None = None,
    tags: list[str] | None = None,
    limit: int = 20,
) -> dict:
    """Search tasks by text query."""

def task_create(
    title: str,
    description: str | None = None,
    priority: str = "normal",
    due_date: datetime | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Create a new task."""

def task_update(
    task_id: UUID,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    due_date: datetime | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Update an existing task."""

def task_complete(task_id: UUID) -> dict:
    """Mark a task as completed (shorthand)."""

def task_delete(task_id: UUID) -> dict:
    """Delete a task."""

def task_bulk_update(
    task_ids: list[UUID],
    status: str | None = None,
    tags_add: list[str] | None = None,
    tags_remove: list[str] | None = None,
) -> dict:
    """Bulk update multiple tasks."""
```

### Implementation Files

```
apps/zerg/backend/zerg/models/user_task.py           # NEW - SQLAlchemy model
apps/zerg/backend/zerg/tools/builtin/task_tools.py   # NEW - 7 tools
apps/zerg/backend/tests/test_task_tools.py           # NEW - tests
apps/zerg/backend/alembic/versions/xxx_user_tasks.py # NEW - migration
apps/zerg/backend/zerg/tools/builtin/__init__.py     # MODIFY - register
apps/zerg/backend/zerg/services/supervisor_service.py # MODIFY - allowlist
apps/zerg/backend/zerg/services/worker_runner.py     # MODIFY - allowlist
```

### Estimate: 2 dev days

---

## Feature 2: Agent Persistent Memory

### Decision Summary

- **Architecture**: Hybrid (Option D) - Per-user Postgres schemas + predefined KV table
- **Data sharing**: Shared at user level by default (all user's agents see same memory)
- **Quotas (free tier)**: 5 tables, 10,000 rows/table, 25 MB storage
- **Expiration**: No auto-expiration for SQL; optional `expires_at` for KV entries
- **Export**: MVP = KV export only via `agent_memory_kv_export()`

### Threat Model

| Threat                                               | Mitigation                                          |
| ---------------------------------------------------- | --------------------------------------------------- |
| **Capability escape** (access public.\*, pg_catalog) | Force `search_path`, reject schema-qualified refs   |
| **SQL injection**                                    | Parameterized queries only, no string interpolation |
| **DoS via long queries**                             | `statement_timeout` (5s default), row limits        |
| **Schema pollution**                                 | Allowlist SQL verbs, block dangerous DDL            |
| **Cross-user access**                                | Schema names include user_id, RLS as future upgrade |

### Architecture Options (for reference)

| Option | Approach                  | Chosen?               |
| ------ | ------------------------- | --------------------- |
| A      | Per-user Postgres schemas | ✅ Phase 1            |
| B      | Per-user SQLite files     | ❌                    |
| C      | Key-Value only            | ✅ Included in hybrid |
| D      | Hybrid (A + C)            | ✅ **Selected**       |
| E      | Shared tables + RLS       | Future upgrade path   |

### SQL Sandbox Rules (Critical)

```python
# Allowlist - ONLY these SQL operations permitted
ALLOWED_SQL_VERBS = {
    "SELECT", "INSERT", "UPDATE", "DELETE",
    "CREATE TABLE", "CREATE INDEX",
    "ALTER TABLE",  # For adding columns
}

# Blocklist - NEVER allow
BLOCKED_SQL_PATTERNS = [
    r"DROP\s+SCHEMA",
    r"CREATE\s+EXTENSION",
    r"CREATE\s+FUNCTION",
    r"CREATE\s+TRIGGER",
    r"COPY\s+",
    r"VACUUM",
    r"ANALYZE",
    r"DO\s+\$",          # Anonymous code blocks
    r"SET\s+",           # Session variables
    r"GRANT",
    r"REVOKE",
    r";\s*\w",           # Multi-statement (semicolon followed by more SQL)
]

# Schema isolation - reject any explicit schema references
BLOCKED_SCHEMA_PATTERNS = [
    r"public\.",
    r"pg_catalog\.",
    r"information_schema\.",
    r"memory_[a-f0-9]+\.",  # Other user schemas
]

# Execution limits
SQL_LIMITS = {
    "statement_timeout": "5s",      # Max query duration
    "max_rows_returned": 1000,      # Cap SELECT results
    "max_response_bytes": 1_000_000, # 1MB response cap
}
```

### Database Schema

```sql
-- Registry table (in public schema)
CREATE TABLE agent_memory_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    schema_name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_accessed_at TIMESTAMPTZ,
    storage_bytes BIGINT DEFAULT 0,
    table_count INT DEFAULT 0,
    UNIQUE(user_id)
);

-- Each user gets their own schema created on first access
-- Example: CREATE SCHEMA memory_abc123def456;

-- Predefined KV table in each user schema
CREATE TABLE _kv (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    tags TEXT[] DEFAULT '{}',
    expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX _kv_tags_idx ON _kv USING GIN(tags);
CREATE INDEX _kv_expires_idx ON _kv(expires_at) WHERE expires_at IS NOT NULL;
```

### Tools

```python
# Location: apps/zerg/backend/zerg/tools/builtin/agent_memory.py

# === SQL Interface (power users) ===

def agent_memory_sql(
    query: str,                    # SQL query (SELECT/INSERT/UPDATE/DELETE/CREATE)
    params: list | None = None,    # Query parameters (required for values)
) -> dict:
    """
    Execute SQL on your persistent memory database.

    Your memory is isolated to your account. You can create tables,
    insert data, and query freely within your schema.

    Returns:
        ok: bool
        rows: list[dict]           # For SELECT
        affected_rows: int         # For INSERT/UPDATE/DELETE
        columns: list[str]         # Column names
        error: str                 # If failed
    """

def agent_memory_schema() -> dict:
    """
    Get the schema of your memory database.

    Returns:
        tables: list[{name, columns: [{name, type, nullable}], row_count}]
        total_storage_bytes: int
        quota_used_percent: float
    """

# === Key-Value Interface (simple use cases) ===

def agent_memory_set(
    key: str,
    value: Any,                    # JSON-serializable
    tags: list[str] | None = None,
    expires_at: datetime | None = None,
) -> dict:
    """Store a value in memory with optional tags and expiration."""

def agent_memory_get(
    key: str | None = None,        # Get specific key
    tags: list[str] | None = None, # Or filter by tags
    limit: int = 100,
) -> dict:
    """Retrieve values by key or tags."""

def agent_memory_delete(
    key: str | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Delete entries by key or tags. Returns count deleted."""

def agent_memory_kv_export() -> dict:
    """Export all KV entries as JSON (size-limited to 5MB)."""
```

### Implementation Files

```
apps/zerg/backend/zerg/models/agent_memory.py            # NEW - registry model
apps/zerg/backend/zerg/services/agent_memory_service.py  # NEW - core service
apps/zerg/backend/zerg/tools/builtin/agent_memory.py     # NEW - 6 tools
apps/zerg/backend/tests/test_agent_memory.py             # NEW - tests
apps/zerg/backend/alembic/versions/xxx_agent_memory.py   # NEW - migration
apps/zerg/backend/zerg/tools/builtin/__init__.py         # MODIFY - register
apps/zerg/backend/zerg/services/supervisor_service.py    # MODIFY - allowlist
apps/zerg/backend/zerg/services/worker_runner.py         # MODIFY - allowlist
```

### Quotas

| Tier       | Max Tables | Max Rows/Table | Max Storage |
| ---------- | ---------- | -------------- | ----------- |
| Free       | 5          | 10,000         | 25 MB       |
| Pro        | 50         | 100,000        | 500 MB      |
| Enterprise | Unlimited  | Unlimited      | 5 GB        |

### Estimate: 4-5 dev days

---

## Feature 3: Webhook Triggers

### Decision Summary

- **Secrets**: Generated by default; explicit opt-out for `insecure_allow_unverified=true`
- **Signature**: HMAC-SHA256 with timestamp (Stripe-style) for replay protection
- **Rate limits**: 60/min per webhook, 300/min per user
- **Payload limit**: 256 KB
- **Retries**: None at Zerg layer; return 202 Accepted and queue work
- **Audit**: Yes, minimal (no full payload storage by default)

### Threat Model

| Threat                    | Mitigation                                     |
| ------------------------- | ---------------------------------------------- |
| **Replay attacks**        | Timestamped signatures with 5-minute tolerance |
| **Spam/abuse**            | Rate limiting per webhook + per user           |
| **Secret leakage**        | Secrets hashed in DB, shown once on creation   |
| **Resource exhaustion**   | Payload size limit, queue-based processing     |
| **Unauthorized triggers** | HMAC validation default-on                     |

### Signature Spec (Stripe-style)

```
Header: X-Swarmlet-Signature: t=1702900000,v1=abc123...

Signature computation:
  payload = f"{timestamp}.{raw_body}"
  signature = HMAC_SHA256(secret, payload.encode()).hexdigest()

Validation:
  1. Parse header to extract t= and v1=
  2. Check timestamp within tolerance (±5 minutes)
  3. Compute expected signature
  4. Constant-time compare
  5. Reject if (webhook_id, signature) seen in last 5 minutes (replay)
```

### Database Schema

```sql
CREATE TABLE webhook_triggers (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    workflow_id UUID NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,

    title TEXT NOT NULL,
    secret_hash TEXT,              -- bcrypt hash of secret (NULL = insecure mode)
    is_active BOOLEAN DEFAULT TRUE,

    -- Rate limiting state
    call_count_minute INT DEFAULT 0,
    minute_window_start TIMESTAMPTZ,

    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE webhook_audit_log (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    webhook_id UUID NOT NULL REFERENCES webhook_triggers(id) ON DELETE CASCADE,

    -- Request info (no full payload by default)
    source_ip INET,
    payload_size_bytes INT,
    payload_hash TEXT,             -- SHA256 of payload for debugging
    headers_subset JSONB,          -- Selected headers only

    -- Validation
    signature_valid BOOLEAN,
    rejection_reason TEXT,         -- NULL if accepted

    -- Result
    run_id UUID,                   -- Resulting workflow run (if triggered)
    http_status INT,               -- Response code sent

    created_at TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX webhook_audit_webhook_idx ON webhook_audit_log(webhook_id, created_at DESC);

-- Replay protection (short-lived)
CREATE TABLE webhook_seen_signatures (
    webhook_id UUID NOT NULL,
    signature_hash TEXT NOT NULL,
    seen_at TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (webhook_id, signature_hash)
);
-- Periodic cleanup: DELETE WHERE seen_at < NOW() - INTERVAL '10 minutes'
```

### API Endpoint

```python
# Location: apps/zerg/backend/zerg/routers/webhooks.py

@router.post("/webhooks/{webhook_id}")
async def receive_webhook(
    webhook_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive incoming webhook and trigger associated workflow.

    Response codes:
    - 202 Accepted: Webhook valid, workflow queued
    - 400 Bad Request: Invalid payload
    - 401 Unauthorized: Invalid/missing signature
    - 404 Not Found: Webhook not found or inactive
    - 413 Payload Too Large: Exceeds 256KB
    - 429 Too Many Requests: Rate limited
    """
```

### Tools

```python
# Location: apps/zerg/backend/zerg/tools/builtin/webhook_tools.py

def webhook_create(
    title: str,
    workflow_id: UUID,
    insecure_allow_unverified: bool = False,
) -> dict:
    """
    Create a webhook trigger for a workflow.

    Returns:
        webhook_id: UUID
        url: str                   # Full webhook URL
        secret: str | None         # Secret (shown ONCE, save it!)
    """

def webhook_list() -> dict:
    """List all webhooks for the current user."""

def webhook_get(webhook_id: UUID) -> dict:
    """Get webhook details (excludes secret)."""

def webhook_delete(webhook_id: UUID) -> dict:
    """Delete a webhook trigger."""

def webhook_rotate_secret(webhook_id: UUID) -> dict:
    """Generate a new secret for a webhook. Returns new secret (once)."""

def webhook_get_audit_log(
    webhook_id: UUID,
    limit: int = 50,
) -> dict:
    """Get recent webhook calls for debugging."""
```

### Implementation Files

```
apps/zerg/backend/zerg/models/webhook.py             # NEW - models
apps/zerg/backend/zerg/routers/webhooks.py           # NEW - endpoint
apps/zerg/backend/zerg/services/webhook_service.py   # NEW - validation/dispatch
apps/zerg/backend/zerg/tools/builtin/webhook_tools.py # NEW - 6 tools
apps/zerg/backend/tests/test_webhooks.py             # NEW - tests
apps/zerg/backend/alembic/versions/xxx_webhooks.py   # NEW - migration
apps/zerg/backend/zerg/tools/builtin/__init__.py     # MODIFY - register
```

### Rate Limits

| Scope                   | Limit               |
| ----------------------- | ------------------- |
| Per webhook             | 60 requests/minute  |
| Per user (all webhooks) | 300 requests/minute |
| Payload size            | 256 KB              |
| Signature tolerance     | ±5 minutes          |

### Estimate: 3 dev days

---

## Implementation Plan

### Phase 1: User Tasks (2 days)

```
Day 1:
- [ ] Create UserTask model with audit fields
- [ ] Write migration
- [ ] Implement task_list, task_create, task_update, task_delete
- [ ] Write tests for basic CRUD

Day 2:
- [ ] Implement task_search, task_complete, task_bulk_update
- [ ] Add to allowed_tools
- [ ] Write remaining tests
- [ ] Commit and push
```

### Phase 2: Agent Memory (4-5 days)

```
Day 1:
- [ ] Create agent_memory_registry model
- [ ] Write migration for registry + KV table template
- [ ] Implement AgentMemoryService (schema creation, search_path forcing)

Day 2:
- [ ] Implement SQL sandbox (allowlist, blocklist, limits)
- [ ] Implement agent_memory_sql with full validation
- [ ] Write security tests (injection attempts, schema escape)

Day 3:
- [ ] Implement agent_memory_schema
- [ ] Implement KV tools (set, get, delete)
- [ ] Write KV tests

Day 4:
- [ ] Implement agent_memory_kv_export
- [ ] Add quota enforcement
- [ ] Add to allowed_tools
- [ ] Integration tests

Day 5 (buffer):
- [ ] Edge cases, cleanup, documentation
```

### Phase 3: Webhook Triggers (3 days)

```
Day 1:
- [ ] Create webhook models (trigger, audit_log, seen_signatures)
- [ ] Write migration
- [ ] Implement signature validation (HMAC + timestamp)
- [ ] Write signature tests

Day 2:
- [ ] Implement POST /webhooks/{id} endpoint
- [ ] Add rate limiting
- [ ] Add audit logging
- [ ] Implement replay protection

Day 3:
- [ ] Implement webhook tools (create, list, delete, rotate_secret, get_audit_log)
- [ ] Add to allowed_tools
- [ ] Integration tests
- [ ] Commit and push
```

---

## Summary of Final Decisions

| #   | Decision             | Choice                                             |
| --- | -------------------- | -------------------------------------------------- |
| 1   | Implementation order | Tasks → Memory → Webhooks                          |
| 2   | Memory architecture  | Hybrid (per-user schemas + KV)                     |
| 3   | Memory quotas (free) | 5 tables, 10K rows, 25MB                           |
| 4   | Memory sharing       | User-level (all agents share)                      |
| 5   | SQL sandbox          | Allowlist verbs, block patterns, force search_path |
| 6   | Webhook security     | HMAC default + timestamped signatures              |
| 7   | Webhook rate limits  | 60/min/webhook, 300/min/user                       |
| 8   | Task scope           | Tool-only MVP                                      |
| 9   | Task audit           | source, origin_run_id, updated_by_agent_id         |

---

## Dependencies

```toml
# No new dependencies required for Tasks or Webhooks

# For Memory (if using advanced SQL parsing):
# sqlparse = "^0.5.0"  # Optional: for SQL statement parsing
```

---

## Future Considerations

1. **Memory → RLS upgrade**: When schema count becomes operationally expensive, migrate to shared tables + Row Level Security
2. **Tasks UI**: Add dashboard page when users request it
3. **Webhooks retries**: Add optional retry queue for failed workflows
4. **Memory export**: Full SQL dump for power users
5. **External task sync**: Todoist, Google Tasks, etc.
