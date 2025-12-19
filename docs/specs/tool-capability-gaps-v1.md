# Tool Capability Gap Analysis & Implementation Plan

**Status**: Draft
**Created**: 2025-12-17
**Author**: Competitive analysis session

## Executive Summary

This document analyzes Zerg/Swarmlet's tool capabilities against a competitor's offering and outlines implementation plans for closing critical gaps. The goal is to enable truly autonomous agents that can research, remember, and communicate—aligning with our "Trust the AI" architecture philosophy.

---

## Part 1: Current State Analysis

### Zerg's Existing Tool Inventory (~80+ tools)

#### Built-in Tools (5)

| Tool                                                        | Description                                 |
| ----------------------------------------------------------- | ------------------------------------------- |
| `get_current_time()`                                        | Returns current UTC time in ISO-8601 format |
| `datetime_diff(start_time, end_time, unit)`                 | Calculate time differences                  |
| `math_eval(expression)`                                     | Safely evaluate mathematical expressions    |
| `generate_uuid(version, namespace, name)`                   | Generate UUIDs (v1, v3, v4, v5)             |
| `http_request(url, method, params, data, headers, timeout)` | Make HTTP requests                          |

#### Communication Tools (5)

| Tool                                                      | Description            | Provider     |
| --------------------------------------------------------- | ---------------------- | ------------ |
| `send_email(to, subject, text, html, ...)`                | Send emails            | Resend API   |
| `send_slack_webhook(text, webhook_url, blocks, ...)`      | Send Slack messages    | Webhook      |
| `send_discord_webhook(content, webhook_url, embeds, ...)` | Send Discord messages  | Webhook      |
| `send_sms(to_number, message, ...)`                       | Send SMS               | Twilio       |
| `send_imessage(address, message, ...)`                    | Send iMessages         | macOS native |
| `list_imessage_messages(address, limit, since_hours)`     | Query iMessage history | macOS native |

#### Integration Tools (~26)

| Service | Tools | Key Operations                                                                                             |
| ------- | ----- | ---------------------------------------------------------------------------------------------------------- |
| GitHub  | 7     | list_repositories, create_issue, list_issues, get_issue, add_comment, list_pull_requests, get_pull_request |
| Jira    | 6     | create_issue, list_issues, get_issue, add_comment, transition_issue, update_issue                          |
| Linear  | 7     | create_issue, list_issues, get_issue, update_issue, add_comment, list_teams                                |
| Notion  | 6     | create_page, get_page, update_page, search, query_database, append_blocks                                  |

#### Execution Tools (4)

| Tool                                               | Description                     | Security                                      |
| -------------------------------------------------- | ------------------------------- | --------------------------------------------- |
| `ssh_exec(host, command, timeout_secs)`            | Execute on remote servers       | Host allowlist: cube, clifford, zerg, slim    |
| `container_exec(command, timeout_secs)`            | Run in ephemeral container      | Read-only FS, no network, /workspace writable |
| `runner_exec(target, command, timeout_secs)`       | Execute on user-managed runners | Multi-tenant safe                             |
| `spawn_worker(task, model, wait, timeout_seconds)` | Spawn disposable worker agents  | Fire-and-forget or monitored                  |

#### Worker Management Tools (5)

| Tool                                  | Description                   |
| ------------------------------------- | ----------------------------- |
| `read_worker_result(job_id)`          | Get worker's final result     |
| `read_worker_file(job_id, file_path)` | Retrieve worker output files  |
| `get_worker_metadata(job_id)`         | Get worker execution metadata |
| `grep_workers(pattern, since_hours)`  | Search worker logs            |
| `list_workers(limit, offset)`         | List recent worker jobs       |

#### Knowledge Tools (1)

| Tool                             | Description                                      |
| -------------------------------- | ------------------------------------------------ |
| `knowledge_search(query, limit)` | Search user's knowledge base (URLs, docs, repos) |

#### System Tools (2)

| Tool                                             | Description                           |
| ------------------------------------------------ | ------------------------------------- |
| `refresh_connector_status()`                     | Check which connectors are configured |
| `runner_list()` / `runner_create_enroll_token()` | Runner management                     |

#### MCP Support

- Full MCP client implementation with health checks, retry logic, HTTP/2 pooling
- Presets for: GitHub, Linear, Slack, Notion, Asana
- Dynamic tool registration from MCP servers
- JSON schema validation for inputs

#### Automation

- Cron-based scheduling via APScheduler
- Email triggers (Gmail watch protocol)
- Webhook triggers (enum exists, not fully implemented)
- Workflow templates and execution tracking

---

## Part 2: Competitor Analysis

### Competitor Tool Categories

```yaml
tools:
  web_search:
    - web_search_web: Search the web for information
    - web_scrape_website: Read webpage as markdown

  file_system:
    - read_file: Read files from /agent/ sandbox
    - apply_patch: Apply structured file changes

  execution:
    - run_command: Execute shell commands in Linux sandbox

  automation:
    - setup_schedule_trigger: Create cron-based tasks
    - setup_webhook_trigger: Listen for POST requests
    - setup_rss_trigger: Monitor RSS/Atom feeds
    - list_triggers / delete_trigger: Manage triggers

  task_management:
    - manage_tasks: CRUD for user's task list
    - list_tasks: View task list

  database:
    - run_agent_memory_sql: Execute SQL on persistent DB
    - get_agent_db_schema: View database schema

  connections:
    - list_users_connections: See existing connections
    - get_details_for_connections: Get connection tools
    - create_new_connections: Create integrations
    - manage_activated_tools_for_connections: Enable/disable tools
    - reauthorize_connection: Re-auth expired connections

  integrations:
    - search_for_integrations: Find by keyword
    - get_integrations_capabilities: Get integration details

  subagents:
    - create_subagent: Create persistent worker config
    - run_subagent: Execute subagent with payload
    - list_subagents / get_subagent / delete_subagent: Manage

  user_interaction:
    - contact_users_via_email: Email agent owners
    - rename_chat: Update chat title
    - get_user_quota_status: Check usage quota
```

---

## Part 3: Gap Analysis Matrix

| Capability                   | Competitor         | Zerg                   | Status           |
| ---------------------------- | ------------------ | ---------------------- | ---------------- |
| **Web Search**               | ✅ Native          | ❌                     | **CRITICAL GAP** |
| **Web Scrape (Markdown)**    | ✅ Native          | ⚠️ http_request (raw)  | **CRITICAL GAP** |
| **Persistent Agent Memory**  | ✅ SQL access      | ❌                     | **CRITICAL GAP** |
| **Contact User**             | ✅ Email to owner  | ❌ System only         | **HIGH GAP**     |
| **Webhook Triggers**         | ✅ Full            | ⚠️ Enum only           | **MEDIUM GAP**   |
| **RSS Triggers**             | ✅                 | ❌                     | LOW GAP          |
| **User Task Management**     | ✅ CRUD            | ❌                     | MEDIUM GAP       |
| **Usage/Quota Visibility**   | ✅                 | ❌                     | LOW GAP          |
| **Persistent Subagents**     | ✅ Named, stored   | ⚠️ Disposable only     | MEDIUM GAP       |
| **Integration Marketplace**  | ✅ Search/discover | ❌                     | LOW GAP          |
|                              |                    |                        |                  |
| **Remote Execution (SSH)**   | ❌                 | ✅                     | **ZERG AHEAD**   |
| **Runner System**            | ❌                 | ✅                     | **ZERG AHEAD**   |
| **MCP Support**              | ❌                 | ✅                     | **ZERG AHEAD**   |
| **First-party Integrations** | ⚠️ Via connections | ✅ Native tools        | **ZERG AHEAD**   |
| **Knowledge Search**         | ❌                 | ✅                     | **ZERG AHEAD**   |
| **iMessage**                 | ❌                 | ✅                     | **ZERG AHEAD**   |
| **Worker Result Inspection** | ⚠️ Basic           | ✅ grep_workers, files | **ZERG AHEAD**   |

---

## Part 4: Implementation Plan

### Priority 1: Web Search & Scrape (CRITICAL)

These are table-stakes for autonomous agents. Without them, workers cannot research anything.

#### 4.1.1 Web Search Tool

**Purpose**: Enable agents to search the internet for information.

**Recommended Provider**: Tavily (built for AI agents) or SerpAPI

**Tool Signature**:

```python
def web_search(
    query: str,                    # Required: Search query
    max_results: int = 5,          # 1-20 results
    search_depth: str = "basic",   # "basic" or "advanced" (Tavily)
    include_domains: list[str] | None = None,  # Limit to domains
    exclude_domains: list[str] | None = None,  # Exclude domains
) -> WebSearchResult:
    """
    Search the web for information.

    Returns:
        results: List of {title, url, snippet, score}
        query: Original query
        response_time_ms: Latency
    """
```

**Implementation Location**: `apps/zerg/backend/zerg/tools/builtin/web_search.py`

**Dependencies**:

```toml
# pyproject.toml
tavily-python = "^0.3.0"  # or serpapi if preferred
```

**Environment Variables**:

```bash
TAVILY_API_KEY=tvly-xxxxx  # or SERPAPI_KEY
```

**Connector Integration**:

- Add `ConnectorType.WEB_SEARCH` to enum
- Store API key in connector credentials
- Tool resolves key from connector or env var fallback

**Cost Consideration**:

- Tavily: $0.01/search (basic), $0.02/search (advanced)
- SerpAPI: $0.005/search
- Consider rate limiting per user/day

**Files to Create/Modify**:

```
apps/zerg/backend/zerg/tools/builtin/web_search.py  # NEW
apps/zerg/backend/zerg/tools/builtin/__init__.py    # Export
apps/zerg/backend/zerg/models/connector.py          # Add enum
apps/zerg/backend/zerg/services/tool_registry.py    # Register
```

---

#### 4.1.2 Web Scrape/Fetch Tool

**Purpose**: Fetch a URL and extract readable content as markdown.

**Recommended Approach**: Use `trafilatura` (best extraction) or `readability-lxml`

**Tool Signature**:

```python
def web_fetch(
    url: str,                      # Required: URL to fetch
    extract_mode: str = "article", # "article", "full", "raw"
    include_links: bool = True,    # Include hyperlinks in markdown
    include_images: bool = False,  # Include image references
    timeout_secs: int = 30,        # Request timeout
) -> WebFetchResult:
    """
    Fetch a webpage and extract content as markdown.

    Modes:
    - article: Extract main content only (default)
    - full: Include navigation, sidebars
    - raw: Return raw HTML

    Returns:
        url: Final URL (after redirects)
        title: Page title
        content: Extracted markdown
        word_count: Approximate word count
        fetch_time_ms: Latency
    """
```

**Implementation Location**: `apps/zerg/backend/zerg/tools/builtin/web_fetch.py`

**Dependencies**:

```toml
trafilatura = "^1.6.0"
httpx = "^0.25.0"  # Already have this
```

**Security Considerations**:

- Block private IP ranges (SSRF protection)
- Timeout enforcement
- Max response size (e.g., 5MB)
- User-Agent identification
- Respect robots.txt (optional flag)

**Implementation Pattern**:

```python
import trafilatura
import httpx
from urllib.parse import urlparse
import ipaddress

BLOCKED_HOSTS = ["localhost", "127.0.0.1", "0.0.0.0"]
BLOCKED_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
]

async def web_fetch(url: str, ...) -> WebFetchResult:
    # 1. Validate URL
    parsed = urlparse(url)
    if parsed.hostname in BLOCKED_HOSTS:
        raise ValidationError("Cannot fetch localhost URLs")

    # 2. Check for private IP
    try:
        ip = ipaddress.ip_address(parsed.hostname)
        for network in BLOCKED_RANGES:
            if ip in network:
                raise ValidationError("Cannot fetch private network URLs")
    except ValueError:
        pass  # Not an IP, proceed with DNS

    # 3. Fetch with httpx
    async with httpx.AsyncClient(timeout=timeout_secs) as client:
        response = await client.get(url, follow_redirects=True)

    # 4. Extract content
    if extract_mode == "raw":
        content = response.text
    else:
        content = trafilatura.extract(
            response.text,
            include_links=include_links,
            include_images=include_images,
            output_format="markdown",
        )

    return WebFetchResult(
        url=str(response.url),
        title=extract_title(response.text),
        content=content,
        word_count=len(content.split()),
        fetch_time_ms=response.elapsed.total_seconds() * 1000,
    )
```

**Files to Create/Modify**:

```
apps/zerg/backend/zerg/tools/builtin/web_fetch.py   # NEW
apps/zerg/backend/zerg/tools/builtin/__init__.py    # Export
apps/zerg/backend/zerg/services/tool_registry.py    # Register
```

---

### Priority 2: Agent Persistent Memory (CRITICAL)

Agents need to remember information across sessions. This enables learning, progress tracking, and stateful workflows.

#### 4.2.1 Design Options

**Option A: Per-User SQLite** (Simpler)

- Each user gets a SQLite file
- Stored in object storage or local disk
- Agents can create tables, run queries
- Pros: Full SQL, isolated, no schema conflicts
- Cons: No cross-agent queries, backup complexity

**Option B: Scoped Postgres Schema** (Recommended)

- Each user gets a schema in main Postgres
- Table naming convention: `memory_{user_id}.{table_name}`
- Shared infrastructure, easier backups
- Pros: Familiar infra, transactions, better tooling
- Cons: Need careful isolation

**Option C: Key-Value Store** (Simplest)

- JSON documents with tags
- Like a simple note-taking system
- Pros: No SQL knowledge needed
- Cons: Limited query capabilities

**Recommended**: Option B (Scoped Postgres) for power users, with Option C as a simplified interface.

#### 4.2.2 Tool Signatures

**Low-level SQL Access**:

```python
def agent_memory_sql(
    query: str,                    # SQL query (SELECT, INSERT, UPDATE, DELETE)
    params: list | None = None,    # Query parameters (prevents injection)
) -> AgentMemoryResult:
    """
    Execute SQL on your persistent memory database.

    Your memory schema is isolated to your account.
    You can create tables, insert data, and query freely.

    Example:
        agent_memory_sql("CREATE TABLE IF NOT EXISTS notes (id SERIAL, content TEXT)")
        agent_memory_sql("INSERT INTO notes (content) VALUES ($1)", ["Remember this"])
        agent_memory_sql("SELECT * FROM notes WHERE content LIKE $1", ["%remember%"])

    Returns:
        rows: Query results (for SELECT)
        affected_rows: Number of rows affected (for INSERT/UPDATE/DELETE)
        columns: Column names
    """
```

**Schema Inspection**:

```python
def agent_memory_schema() -> AgentMemorySchema:
    """
    Get the schema of your memory database.

    Returns:
        tables: List of {name, columns: [{name, type, nullable}]}
        total_rows: Total rows across all tables
        storage_used_bytes: Approximate storage used
    """
```

**Simplified Key-Value Interface** (Optional, friendlier):

```python
def agent_memory_set(
    key: str,
    value: Any,                    # JSON-serializable
    tags: list[str] | None = None,
    expires_at: datetime | None = None,
) -> None:
    """Store a value in memory with optional tags and expiration."""

def agent_memory_get(
    key: str | None = None,
    tags: list[str] | None = None,
    limit: int = 100,
) -> list[MemoryEntry]:
    """Retrieve values by key or tags."""

def agent_memory_delete(
    key: str | None = None,
    tags: list[str] | None = None,
) -> int:
    """Delete entries by key or tags. Returns count deleted."""
```

#### 4.2.3 Implementation Details

**Database Schema** (for scoped Postgres approach):

```sql
-- System table to track user memory schemas
CREATE TABLE agent_memory_registry (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    schema_name TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_accessed_at TIMESTAMPTZ,
    storage_bytes BIGINT DEFAULT 0,
    UNIQUE(user_id)
);

-- Each user gets their own schema created on first access
-- Example: CREATE SCHEMA memory_abc123def456;
```

**Security Implementation**:

```python
class AgentMemoryService:
    def __init__(self, db: AsyncSession, user_id: UUID):
        self.db = db
        self.user_id = user_id
        self.schema_name = f"memory_{user_id.hex[:12]}"

    async def ensure_schema(self):
        """Create user's memory schema if it doesn't exist."""
        await self.db.execute(text(f"CREATE SCHEMA IF NOT EXISTS {self.schema_name}"))
        await self.db.execute(text(
            f"ALTER ROLE current_user SET search_path TO {self.schema_name}, public"
        ))

    async def execute_query(self, query: str, params: list | None = None):
        """Execute query in user's isolated schema."""
        # Validate query doesn't escape schema
        if self._contains_schema_escape(query):
            raise SecurityError("Query cannot reference other schemas")

        # Set search path for this connection
        await self.db.execute(text(f"SET search_path TO {self.schema_name}"))

        # Execute with parameterized query
        result = await self.db.execute(text(query), params or {})
        return result

    def _contains_schema_escape(self, query: str) -> bool:
        """Check for attempts to access other schemas."""
        # Block: schema.table, information_schema, pg_catalog, etc.
        dangerous_patterns = [
            r'\b\w+\.\w+',  # schema.table
            r'information_schema',
            r'pg_catalog',
            r'pg_',
            r'memory_(?!{})'.format(self.schema_name),  # Other user schemas
        ]
        # ... pattern matching
```

**Quotas & Limits**:

```python
MEMORY_LIMITS = {
    "free": {
        "max_tables": 10,
        "max_rows_per_table": 10000,
        "max_storage_bytes": 50 * 1024 * 1024,  # 50MB
    },
    "pro": {
        "max_tables": 100,
        "max_rows_per_table": 1000000,
        "max_storage_bytes": 1 * 1024 * 1024 * 1024,  # 1GB
    },
}
```

**Files to Create/Modify**:

```
apps/zerg/backend/zerg/tools/builtin/agent_memory.py     # NEW
apps/zerg/backend/zerg/services/agent_memory_service.py  # NEW
apps/zerg/backend/zerg/models/agent_memory.py            # NEW (registry model)
apps/zerg/backend/alembic/versions/xxx_agent_memory.py   # Migration
apps/zerg/backend/zerg/tools/builtin/__init__.py         # Export
apps/zerg/backend/zerg/services/tool_registry.py         # Register
```

---

### Priority 3: Contact User Tool (HIGH)

Long-running workers need to notify their owners when tasks complete, fail, or need attention.

#### 4.3.1 Tool Signature

```python
def contact_user(
    subject: str,                  # Email subject
    message: str,                  # Message body (markdown supported)
    priority: str = "normal",      # "low", "normal", "high", "urgent"
    channel: str = "email",        # "email", "push" (future), "sms" (future)
) -> ContactResult:
    """
    Send a notification to the agent's owner.

    Use this when:
    - A long-running task completes
    - You encounter an error that needs user attention
    - You need user input to proceed
    - Important events occur that the user should know about

    Priority levels:
    - low: Informational, batched delivery OK
    - normal: Standard delivery
    - high: Immediate delivery
    - urgent: Immediate + may trigger additional channels

    Returns:
        sent: bool
        channel_used: str
        message_id: str (for tracking)
    """
```

#### 4.3.2 Implementation

**Uses Existing Infrastructure**:

- Leverages `send_email` tool internally
- Uses user's email from their profile
- Adds Zerg branding/template

**Template**:

```html
<!-- Agent Notification Email Template -->
<div style="font-family: sans-serif; max-width: 600px; margin: 0 auto;">
  <div style="background: #1a1a2e; color: white; padding: 20px;">
    <h1>🤖 Swarmlet Agent Notification</h1>
  </div>
  <div style="padding: 20px; background: #f5f5f5;">
    <p><strong>From:</strong> {{ agent_name or "Your Agent" }}</p>
    <p><strong>Priority:</strong> {{ priority }}</p>
    <hr />
    <div>{{ message | markdown }}</div>
  </div>
  <div style="padding: 10px; text-align: center; color: #666;">
    <a href="{{ dashboard_url }}">View in Dashboard</a>
  </div>
</div>
```

**Rate Limiting**:

```python
CONTACT_LIMITS = {
    "per_agent_per_hour": 10,
    "per_user_per_day": 50,
    "urgent_per_day": 5,
}
```

**Files to Create/Modify**:

```
apps/zerg/backend/zerg/tools/builtin/contact_user.py  # NEW
apps/zerg/backend/zerg/templates/agent_notification.html  # NEW
apps/zerg/backend/zerg/services/notification_service.py  # NEW or extend
```

---

### Priority 4: Webhook Triggers (MEDIUM)

The enum and infrastructure exists; needs completion.

#### 4.4.1 Current State

```python
# Already exists in codebase:
class RunTrigger(str, Enum):
    manual = "manual"
    webhook = "webhook"    # <- Not fully implemented
    schedule = "schedule"
    email = "email"
```

#### 4.4.2 Implementation Plan

**Webhook Endpoint**:

```python
# apps/zerg/backend/zerg/routers/webhooks.py

@router.post("/webhooks/{webhook_id}")
async def receive_webhook(
    webhook_id: UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Receive incoming webhook and trigger associated workflow.
    """
    # 1. Look up webhook config
    trigger = await db.get(Trigger, webhook_id)
    if not trigger or trigger.type != "webhook":
        raise HTTPException(404, "Webhook not found")

    # 2. Validate signature (if configured)
    if trigger.config.get("secret"):
        signature = request.headers.get("X-Webhook-Signature")
        if not verify_signature(await request.body(), trigger.config["secret"], signature):
            raise HTTPException(401, "Invalid signature")

    # 3. Parse payload
    payload = await request.json()

    # 4. Trigger workflow
    execution = await workflow_service.trigger(
        workflow_id=trigger.workflow_id,
        trigger_type="webhook",
        payload=payload,
        metadata={
            "headers": dict(request.headers),
            "webhook_id": str(webhook_id),
        },
    )

    return {"execution_id": execution.id, "status": "triggered"}
```

**Tool for Creating Webhooks**:

```python
def create_webhook_trigger(
    title: str,                    # Human-readable name
    workflow_id: UUID,             # Workflow to trigger
    secret: str | None = None,     # Optional HMAC secret
    allowed_ips: list[str] | None = None,  # IP allowlist
) -> WebhookTrigger:
    """
    Create a webhook that triggers a workflow when called.

    Returns:
        webhook_id: UUID
        url: Full webhook URL to share
        secret: The secret (only shown once)
    """
```

**Database Model** (extend existing Trigger):

```python
class TriggerConfig(BaseModel):
    # For webhook type:
    secret: str | None = None
    allowed_ips: list[str] | None = None
    last_called_at: datetime | None = None
    call_count: int = 0
```

**Files to Create/Modify**:

```
apps/zerg/backend/zerg/routers/webhooks.py      # NEW or extend
apps/zerg/backend/zerg/tools/builtin/triggers.py  # NEW
apps/zerg/backend/zerg/models/trigger.py        # Extend config
```

---

### Priority 5: User Task Management (MEDIUM)

Enable agents to manage a user's task list.

#### 4.5.1 Design

**New Model**:

```python
class UserTask(Base):
    __tablename__ = "user_tasks"

    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str]
    description: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="pending")  # pending, in_progress, completed, cancelled
    priority: Mapped[str] = mapped_column(default="normal")  # low, normal, high, urgent
    due_date: Mapped[datetime | None]
    tags: Mapped[list[str]] = mapped_column(ARRAY(String), default=[])
    created_by_agent_id: Mapped[UUID | None]  # Which agent created this
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime] = mapped_column(onupdate=func.now())
    completed_at: Mapped[datetime | None]
```

**Tools**:

```python
def task_list(
    status: str | None = None,     # Filter by status
    tags: list[str] | None = None, # Filter by tags
    limit: int = 50,
) -> list[UserTask]:
    """List tasks in your task list."""

def task_create(
    title: str,
    description: str | None = None,
    priority: str = "normal",
    due_date: datetime | None = None,
    tags: list[str] | None = None,
) -> UserTask:
    """Add a task to your task list."""

def task_update(
    task_id: UUID,
    title: str | None = None,
    description: str | None = None,
    status: str | None = None,
    priority: str | None = None,
    due_date: datetime | None = None,
    tags: list[str] | None = None,
) -> UserTask:
    """Update a task."""

def task_delete(task_id: UUID) -> bool:
    """Remove a task from your list."""
```

**Files to Create/Modify**:

```
apps/zerg/backend/zerg/models/user_task.py        # NEW
apps/zerg/backend/zerg/tools/builtin/tasks.py     # NEW
apps/zerg/backend/zerg/routers/tasks.py           # NEW (for UI)
apps/zerg/backend/alembic/versions/xxx_user_tasks.py  # Migration
apps/zerg/frontend-web/src/pages/TasksPage.tsx    # NEW (optional UI)
```

---

## Part 5: Implementation Order

### Phase 1: Research Capabilities (Week 1-2)

1. **web_search** - Tavily integration
2. **web_fetch** - trafilatura extraction

These unblock autonomous research immediately.

### Phase 2: Memory & State (Week 2-3)

3. **agent_memory_sql** - Scoped Postgres schemas
4. **agent_memory_schema** - Schema inspection

Enables learning and stateful workflows.

### Phase 3: Communication (Week 3)

5. **contact_user** - Owner notifications

Enables async worker patterns.

### Phase 4: Automation (Week 4)

6. **Webhook triggers** - Complete existing infrastructure
7. **User tasks** - Task management tools

---

## Part 6: Testing Strategy

### Unit Tests

Each tool needs:

- Happy path test
- Error handling test
- Input validation test
- Rate limiting test (where applicable)

### Integration Tests

- Web search with real API (use test key)
- Web fetch with test URLs
- Memory with isolated test schema
- Contact with mock email service

### E2E Tests

- Agent workflow using web search → memory → contact
- Webhook trigger → workflow execution

---

## Part 7: Cost & Resource Estimates

| Feature             | External Costs                  | Dev Effort |
| ------------------- | ------------------------------- | ---------- |
| Web Search (Tavily) | ~$0.01-0.02/search              | 1-2 days   |
| Web Fetch           | Free (trafilatura)              | 1 day      |
| Agent Memory        | Postgres storage (~$0.10/GB/mo) | 3-4 days   |
| Contact User        | Email via Resend (existing)     | 1 day      |
| Webhook Triggers    | None                            | 2 days     |
| User Tasks          | Postgres storage                | 2-3 days   |

**Total Estimated Effort**: 10-14 dev days

---

## Part 8: Open Questions

1. **Web Search Provider**: Tavily vs SerpAPI vs Brave Search?
   - Tavily: Built for AI, good snippets, $0.01/search
   - SerpAPI: More features, $0.005/search
   - Brave: Privacy-focused, $0.003/search

2. **Memory Quotas**: What limits for free vs paid tiers?

3. **Contact User Channels**: Start with email only, or include push/SMS?

4. **Task Management UI**: Build dashboard page, or tool-only for now?

5. **Webhook Security**: Require secrets, or optional?

---

## Appendix A: Zerg's Competitive Advantages

Areas where Zerg is already ahead:

1. **Remote Execution**: SSH to real servers, runner system for multi-tenant
2. **MCP Support**: Full protocol implementation, presets, dynamic registration
3. **First-party Integrations**: Native GitHub, Jira, Linear, Notion tools
4. **Knowledge Search**: Semantic search across synced content
5. **Worker Management**: Result inspection, grep_workers, file retrieval
6. **iMessage**: Unique capability for personal automation

These should be highlighted in marketing and maintained as differentiators.

---

## Appendix B: Competitor Features NOT to Implement

Some competitor features don't align with Zerg's philosophy:

1. **File System in Sandbox**: Zerg uses container_exec and runners instead. More flexible.
2. **Integration Marketplace**: Over-engineered. MCP + native tools is cleaner.
3. **Persistent Subagents**: Consider later. Current disposable workers + agent configs may suffice.

---

## References

- Competitor analysis: Session 2025-12-17
- Zerg architecture (current): `docs/specs/jarvis-supervisor-unification-v2.1.md` (historical: `docs/specs/super-siri-architecture.md`)
- Tool registry: `apps/zerg/backend/zerg/services/tool_registry.py`
- Existing tools: `apps/zerg/backend/zerg/tools/builtin/`
