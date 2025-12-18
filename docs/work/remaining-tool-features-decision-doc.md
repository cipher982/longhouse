# Remaining Tool Features - Decision Document

**Status**: Awaiting decisions
**Created**: 2025-12-18
**Context**: Competitive analysis identified gaps; web_search, web_fetch, and contact_user have been implemented. Three features remain.

---

## Executive Summary

Three features remain from the tool capability gap analysis:

| Feature                 | Priority | Complexity  | Key Decision Needed     |
| ----------------------- | -------- | ----------- | ----------------------- |
| Agent Persistent Memory | High     | Medium-High | Storage architecture    |
| Webhook Triggers        | Medium   | Medium      | Security model          |
| User Task Management    | Medium   | Low-Medium  | Scope (tool-only vs UI) |

---

## Feature 1: Agent Persistent Memory (`agent_memory_sql`)

### What It Is

A persistent storage system that allows agents to remember information across sessions. Currently, agents have no way to store learned information, preferences, or state between conversations.

### Why It Matters

- **Stateful workflows**: Agents can track progress on multi-session tasks
- **Learning**: Agents can remember user preferences, past interactions
- **Data collection**: Agents can accumulate research, logs, metrics over time
- **Competitive parity**: Competitor offers `run_agent_memory_sql` + `get_agent_db_schema`

### Design Options

#### Option A: Scoped Postgres Schemas (Recommended)

Each user gets their own Postgres schema within the existing database.

```
Main DB
├── public schema (existing tables: users, agents, threads, etc.)
├── memory_abc123 schema (User A's memory tables)
├── memory_def456 schema (User B's memory tables)
└── memory_ghi789 schema (User C's memory tables)
```

**Pros:**

- Uses existing infrastructure (no new services)
- Full SQL power (joins, indexes, aggregations)
- Easy backups (part of main DB backup)
- Transactions work naturally
- Can query across user's own tables

**Cons:**

- Schema isolation requires careful implementation
- Need to prevent SQL injection and schema escaping
- Shared resource contention possible at scale

**Implementation:**

```python
# Tools exposed to agents:
agent_memory_sql(query: str, params: list) -> rows/affected_count
agent_memory_schema() -> {tables, columns, storage_used}
```

#### Option B: Per-User SQLite Files

Each user gets a SQLite database file stored in object storage or disk.

**Pros:**

- Complete isolation (separate files)
- No schema collision possible
- Can be downloaded/exported easily
- Simpler security model

**Cons:**

- New infrastructure (file storage, retrieval)
- No cross-table queries with main DB
- Concurrent access limitations
- Backup complexity increases

#### Option C: Key-Value Store (Simpler Interface)

JSON documents with tags, like a simple note system.

```python
agent_memory_set(key, value, tags, expires_at)
agent_memory_get(key=None, tags=None, limit=100)
agent_memory_delete(key=None, tags=None)
```

**Pros:**

- No SQL knowledge needed
- Simple mental model
- Easy to implement

**Cons:**

- Limited query capabilities
- No relational data
- Less powerful than SQL

#### Option D: Hybrid (Recommended if budget allows)

Offer both:

- `agent_memory_sql` for power users (Option A)
- `agent_memory_*` key-value for simple use cases (Option C)

Both backed by the same Postgres schema, but key-value uses a predefined table structure.

### Security Considerations

1. **SQL Injection**: Must use parameterized queries only
2. **Schema Escaping**: Block queries that reference other schemas (`public.*`, `memory_other.*`)
3. **Resource Limits**: Cap tables, rows, storage per user
4. **Dangerous Operations**: Block `DROP SCHEMA`, `CREATE EXTENSION`, etc.

### Quota Recommendations

| Tier       | Max Tables | Max Rows/Table | Max Storage |
| ---------- | ---------- | -------------- | ----------- |
| Free       | 5          | 10,000         | 25 MB       |
| Pro        | 50         | 100,000        | 500 MB      |
| Enterprise | Unlimited  | Unlimited      | 5 GB        |

### Open Questions

1. **Which option?** A (Postgres schemas), B (SQLite), C (Key-Value), or D (Hybrid)?
2. **What quotas** for free tier?
3. **Should agents see each other's data?** (Same user, different agents)
4. **Expiration policy?** Auto-delete after N days of inactivity?
5. **Export capability?** Let users download their agent's memory?

### Implementation Estimate

- Option A (Postgres): 3-4 dev days
- Option B (SQLite): 4-5 dev days (new infra)
- Option C (Key-Value): 1-2 dev days
- Option D (Hybrid): 4-5 dev days

---

## Feature 2: Webhook Triggers

### What It Is

Allow external services to trigger Zerg workflows by calling a webhook URL. The infrastructure partially exists (enum, trigger model) but isn't fully implemented.

### Why It Matters

- **Event-driven automation**: GitHub push → run tests, Stripe payment → send welcome email
- **Integration flexibility**: Any service with webhooks can trigger Zerg
- **Competitive parity**: Competitor offers `setup_webhook_trigger`

### Current State

```python
# Already exists:
class RunTrigger(str, Enum):
    manual = "manual"
    webhook = "webhook"    # ← Partially implemented
    schedule = "schedule"
    email = "email"
```

### Design

#### Webhook URL Format

```
POST https://api.swarmlet.com/webhooks/{webhook_id}
```

#### Webhook Configuration

```python
def create_webhook_trigger(
    title: str,              # Human-readable name
    workflow_id: UUID,       # What to trigger
    secret: str | None,      # HMAC signing secret (optional)
) -> {webhook_id, url, secret}
```

#### Security Options

**Option 1: No validation (simplest)**

- Anyone with the URL can trigger
- Rely on URL obscurity (UUID)
- Fine for personal/trusted use

**Option 2: HMAC signature validation (recommended)**

- Caller must sign payload with shared secret
- Standard pattern (GitHub, Stripe, etc.)
- Header: `X-Webhook-Signature: sha256=...`

**Option 3: IP allowlist**

- Only accept from known IPs
- Hard to maintain, breaks with dynamic IPs

**Recommendation**: Option 2 (HMAC) with Option 1 as fallback for simple cases.

#### Payload Handling

The webhook payload becomes available to the workflow as input variables:

```python
# Incoming webhook
POST /webhooks/abc123
{
  "event": "push",
  "repository": "user/repo",
  "branch": "main"
}

# Workflow receives:
trigger_payload = {
  "event": "push",
  "repository": "user/repo",
  "branch": "main"
}
```

### Open Questions

1. **Require secrets?** Or make them optional?
2. **Rate limiting?** Per-webhook or per-user?
3. **Payload size limit?** (Suggest 1MB max)
4. **Retry on failure?** If workflow fails, retry webhook?
5. **Logging/audit?** Store webhook call history?

### Implementation Estimate

2-3 dev days

---

## Feature 3: User Task Management

### What It Is

Allow agents to manage a task list for the user. Agents can add, update, complete, and query tasks.

### Why It Matters

- **Personal assistant use case**: "Add buy groceries to my list"
- **Agent accountability**: Agents can track what they promised to do
- **Workflow coordination**: Multiple agents can see shared task state
- **Competitive parity**: Competitor offers `manage_tasks`, `list_tasks`

### Design

#### Data Model

```python
class UserTask(Base):
    id: UUID
    user_id: UUID              # Owner
    title: str
    description: str | None
    status: str                # pending, in_progress, completed, cancelled
    priority: str              # low, normal, high, urgent
    due_date: datetime | None
    tags: list[str]
    created_by_agent_id: UUID | None  # Which agent created this
    created_at: datetime
    completed_at: datetime | None
```

#### Tools

```python
def task_list(status: str = None, tags: list = None, limit: int = 50) -> list[Task]
def task_create(title: str, description: str = None, priority: str = "normal",
                due_date: datetime = None, tags: list = None) -> Task
def task_update(task_id: UUID, **updates) -> Task
def task_delete(task_id: UUID) -> bool
def task_complete(task_id: UUID) -> Task  # Shorthand for status update
```

### Scope Options

**Option 1: Tool-only (MVP)**

- Just the tools, no UI
- Users interact via agents only
- Fast to implement

**Option 2: Tool + Basic UI**

- Add a Tasks page to the dashboard
- Simple list view with filters
- Agents and UI can both modify

**Option 3: Full Task System**

- Rich UI with drag-drop, calendar view
- Subtasks, dependencies, assignments
- Basically building a todo app

**Recommendation**: Start with Option 1, add Option 2 if users request it.

### Open Questions

1. **Scope**: Tool-only or include UI?
2. **Sharing**: Can tasks be shared between users? (Probably not for MVP)
3. **Notifications**: Alert user when agent adds urgent task?
4. **Recurrence**: Support recurring tasks? (Probably not for MVP)
5. **Integration**: Sync with external task systems (Todoist, etc.)? (Future)

### Implementation Estimate

- Option 1 (Tool-only): 2 dev days
- Option 2 (Tool + Basic UI): 4-5 dev days
- Option 3 (Full system): 2+ weeks

---

## Prioritization Recommendation

Based on impact vs effort:

### Recommended Order

1. **Agent Memory (Option A or D)** - High impact, enables stateful agents
2. **User Tasks (Option 1)** - Quick win, natural assistant capability
3. **Webhook Triggers** - Important but can wait, schedule triggers exist

### Alternative: Quick Wins First

1. **User Tasks (Option 1)** - 2 days, immediate utility
2. **Agent Memory (Option C)** - 2 days, key-value is still useful
3. **Webhook Triggers** - 3 days
4. **Upgrade to full SQL memory later**

---

## Summary of Decisions Needed

| #   | Question             | Options                         | Recommendation                    |
| --- | -------------------- | ------------------------------- | --------------------------------- |
| 1   | Memory architecture  | Postgres / SQLite / KV / Hybrid | Postgres (A) or Hybrid (D)        |
| 2   | Memory quotas        | Various                         | 5 tables, 10K rows, 25MB for free |
| 3   | Webhook security     | None / HMAC / IP                | HMAC with optional fallback       |
| 4   | Task scope           | Tool-only / +UI / Full          | Tool-only for MVP                 |
| 5   | Implementation order | Memory→Tasks→Webhooks           | Memory first (highest impact)     |

---

## Next Steps

Once decisions are made:

1. Finalize design based on chosen options
2. Write detailed implementation spec
3. Implement with TDD approach (tests first)
4. Add to allowed_tools for workers/supervisors
5. Update documentation

Please review and provide decisions on the open questions above.
