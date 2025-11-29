# Implementation Roadmap: 100% Scenario Coverage

> Gaps sorted by implementation effort, grouped into sprints

---

## Summary Matrix

| Effort | Count | Time Est. | Value |
|--------|-------|-----------|-------|
| 🟢 Easy Wins | 12 items | 1-2 weeks | High - Quick user value |
| 🟡 Medium Effort | 10 items | 3-4 weeks | High - Core features |
| 🔴 Complex/Long | 8 items | 6-8 weeks | Medium - Power users |

---

## 🟢 EASY WINS (1-3 days each)

These require minimal new infrastructure and can be shipped quickly.

### Week 1: Core Output Tools

| # | Feature | Effort | Description | Dependencies |
|---|---------|--------|-------------|--------------|
| 1 | **Slack Webhook Tool** | 2 hrs | Wrap `http_request` with Slack message formatting | None |
| 2 | **Discord Webhook Tool** | 2 hrs | Wrap `http_request` with Discord embed formatting | None |
| 3 | **Email via API** | 4 hrs | SendGrid/Mailgun/Resend API wrapper | API key config |
| 4 | **SMS via Twilio** | 4 hrs | Simple Twilio API wrapper | API key config |

```python
# Implementation sketch - all are thin wrappers
def send_slack_webhook(webhook_url: str, text: str, blocks: dict = None) -> dict:
    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks
    return http_request(webhook_url, method="POST", data=payload)
```

**Why easy:** These are literally specialized `http_request` calls with better DX.

---

### Week 1: Simple State/Memory

| # | Feature | Effort | Description | Dependencies |
|---|---------|--------|-------------|--------------|
| 5 | **Key-Value Store** | 1 day | New `agent_state` table + CRUD tools | DB migration |
| 6 | **State TTL/Expiry** | 2 hrs | Add `expires_at` column, cleanup job | #5 |

```sql
-- Simple schema
CREATE TABLE agent_state (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER REFERENCES agents(id),
    key VARCHAR(255) NOT NULL,
    value JSONB NOT NULL,
    expires_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    UNIQUE(agent_id, key)
);
```

```python
# Tools to add
def kv_get(key: str) -> Any: ...
def kv_set(key: str, value: Any, ttl_seconds: int = None) -> bool: ...
def kv_delete(key: str) -> bool: ...
def kv_list(prefix: str = None) -> List[str]: ...
```

**Why easy:** Just a new table + 4 simple tools. No external deps.

---

### Week 1: Basic File Operations

| # | Feature | Effort | Description | Dependencies |
|---|---------|--------|-------------|--------------|
| 7 | **Write File** | 4 hrs | Write to agent workspace directory | Storage config |
| 8 | **Read File** | 2 hrs | Read from agent workspace | #7 |
| 9 | **List Files** | 2 hrs | List workspace contents | #7 |
| 10 | **Generate CSV** | 2 hrs | Python stdlib csv module | None |

```python
def write_file(filename: str, content: str) -> dict:
    """Write content to agent's workspace directory."""
    workspace = get_agent_workspace(current_agent_id)
    path = workspace / sanitize_filename(filename)
    path.write_text(content)
    return {"path": str(path), "size": len(content)}

def generate_csv(data: List[Dict], filename: str) -> dict:
    """Generate CSV from list of dicts and save to workspace."""
    import csv, io
    output = io.StringIO()
    if data:
        writer = csv.DictWriter(output, fieldnames=data[0].keys())
        writer.writeheader()
        writer.writerows(data)
    return write_file(filename, output.getvalue())
```

**Why easy:** Python stdlib, just need workspace directory management.

---

### Week 2: Platform Basics

| # | Feature | Effort | Description | Dependencies |
|---|---------|--------|-------------|--------------|
| 11 | **Audit Logs** | 4 hrs | Log tool invocations to new table | DB migration |
| 12 | **Rate Limit per Tool** | 4 hrs | Token bucket per agent/tool | Redis or DB |

```sql
CREATE TABLE audit_logs (
    id SERIAL PRIMARY KEY,
    agent_id INTEGER,
    run_id INTEGER,
    tool_name VARCHAR(255),
    input JSONB,
    output JSONB,
    duration_ms INTEGER,
    created_at TIMESTAMP DEFAULT NOW()
);
```

**Why easy:** Already have event system, just persist more.

---

## 🟡 MEDIUM EFFORT (3-7 days each)

These require some new infrastructure but are well-understood patterns.

### Week 3-4: Enhanced Outputs

| # | Feature | Effort | Est. | Description |
|---|---------|--------|------|-------------|
| 13 | **Email via SMTP** | 3 days | Native SMTP support (no API dependency) |
| 14 | **PDF Generation** | 3 days | Headless Chrome or WeasyPrint |
| 15 | **S3 Upload** | 2 days | boto3 integration with creds |
| 16 | **Image Generation** | 3 days | DALL-E/Stable Diffusion API wrapper |

```python
# PDF via WeasyPrint (pure Python, no Chrome needed)
def generate_pdf(html_content: str, filename: str) -> dict:
    from weasyprint import HTML
    pdf_bytes = HTML(string=html_content).write_pdf()
    return write_file(filename, pdf_bytes, binary=True)
```

**Considerations:**
- SMTP needs server config (host, port, auth)
- PDF needs either WeasyPrint (lighter) or Playwright (full Chrome)
- S3 needs secure credential storage

---

### Week 4-5: Connectors (API Key Auth)

| # | Feature | Effort | Est. | Description |
|---|---------|--------|------|-------------|
| 17 | **GitHub Connector** | 3 days | REST API wrapper with PAT auth |
| 18 | **Jira Connector** | 3 days | REST API for issues/comments |
| 19 | **Linear Connector** | 2 days | GraphQL API wrapper |
| 20 | **Notion Connector** | 2 days | REST API for pages/databases |

```python
# Connector pattern
class GitHubConnector:
    def __init__(self, api_token: str):
        self.token = api_token
        self.base_url = "https://api.github.com"
    
    async def create_issue(self, repo: str, title: str, body: str) -> dict: ...
    async def list_issues(self, repo: str, state: str = "open") -> List[dict]: ...
    async def add_comment(self, repo: str, issue_number: int, body: str) -> dict: ...
```

**Why medium:** 
- API design is straightforward
- Need secure token storage (already have crypto utils)
- Each connector is ~200-400 lines

---

### Week 5-6: Workflow Features

| # | Feature | Effort | Est. | Description |
|---|---------|--------|------|-------------|
| 21 | **Scheduled Actions** | 4 days | Queue future tool executions |
| 22 | **Multi-Agent Calls** | 3 days | Tool to invoke another agent |

```python
# Scheduled action tool
def schedule_action(
    action: str,  # "send_email", "send_slack", etc.
    params: dict,
    run_at: datetime,
    idempotency_key: str = None
) -> dict:
    """Schedule a tool to run at a future time."""
    job = ScheduledAction(
        agent_id=current_agent_id,
        action=action,
        params=params,
        run_at=run_at,
        idempotency_key=idempotency_key
    )
    db.add(job)
    return {"job_id": job.id, "scheduled_for": run_at.isoformat()}

# Multi-agent tool
def invoke_agent(
    agent_id: int,
    message: str,
    wait_for_completion: bool = True,
    timeout_seconds: int = 300
) -> dict:
    """Invoke another agent and optionally wait for result."""
    # Uses existing execute_agent_task infrastructure
    ...
```

**Why medium:**
- Scheduled actions need a job queue (APScheduler already exists)
- Multi-agent mostly reuses existing AgentRunner

---

## 🔴 COMPLEX/LONG (1-2 weeks each)

These require significant new infrastructure or external integrations.

### Week 7-10: OAuth Connectors

| # | Feature | Effort | Est. | Description |
|---|---------|--------|------|-------------|
| 23 | **Google OAuth Flow** | 5 days | OAuth2 dance, token refresh |
| 24 | **Google Calendar** | 3 days | Calendar API (requires #23) |
| 25 | **Google Sheets** | 3 days | Sheets API (requires #23) |
| 26 | **Google Drive** | 2 days | Drive API (requires #23) |
| 27 | **Slack App OAuth** | 5 days | Slack OAuth, bot tokens |

**Complexity factors:**
- OAuth requires frontend UI for "Connect" flow
- Token refresh logic
- Scope management
- Per-user credential storage

```python
# OAuth infrastructure needed
class OAuthConnector(Base):
    __tablename__ = "oauth_connectors"
    
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    provider = Column(String)  # "google", "slack", "github"
    access_token = Column(String)  # encrypted
    refresh_token = Column(String)  # encrypted
    expires_at = Column(DateTime)
    scopes = Column(JSON)
    created_at = Column(DateTime)
    
# Frontend routes needed
GET  /oauth/{provider}/authorize  -> redirect to provider
GET  /oauth/{provider}/callback   -> exchange code for token
POST /oauth/{provider}/refresh    -> refresh token
```

---

### Week 9-12: Advanced Workflow Features

| # | Feature | Effort | Est. | Description |
|---|---------|--------|------|-------------|
| 28 | **Human-in-the-Loop** | 7 days | Approval nodes with notifications |
| 29 | **Workflow Variables UI** | 5 days | Define inputs, pass between nodes |
| 30 | **Conditional Branching UI** | 3 days | Visual condition builder |

```python
# Human approval node
class ApprovalNodeExecutor(BaseNodeExecutor):
    async def _execute_node_logic(self, db, state, execution_id):
        # 1. Create approval request record
        approval = ApprovalRequest(
            execution_id=execution_id,
            node_id=self.node_id,
            message=self.node.config.get("message"),
            options=self.node.config.get("options", ["Approve", "Reject"]),
            timeout_hours=self.node.config.get("timeout_hours", 24),
            notify_via=self.node.config.get("notify_via", "email"),
        )
        db.add(approval)
        
        # 2. Send notification
        await self._send_approval_notification(approval)
        
        # 3. Pause execution (workflow engine needs to support this)
        raise WorkflowPausedException(
            approval_id=approval.id,
            resume_endpoint=f"/api/approvals/{approval.id}/respond"
        )
```

**Complexity factors:**
- Workflow engine needs pause/resume capability
- Need approval UI (inbox, respond)
- Need notification dispatch
- Timeout handling

---

## 📅 Recommended Sprint Plan

### Sprint 1 (Week 1-2): Foundation
**Goal:** Users can send notifications and store state

| Day | Tasks |
|-----|-------|
| 1 | Slack webhook tool + Discord webhook tool |
| 2 | Email via SendGrid/Resend API |
| 3 | SMS via Twilio |
| 4 | KV store schema + migration |
| 5 | KV store tools (get/set/delete/list) |
| 6 | Write file tool + workspace management |
| 7 | Read file + list files + CSV generation |
| 8 | Testing + documentation |
| 9 | Audit log schema + logging |
| 10 | Rate limiting per tool |

**Deliverable:** 12 new tools, agents can notify and remember

---

### Sprint 2 (Week 3-4): Enhanced Outputs
**Goal:** Rich output formats and cloud storage

| Day | Tasks |
|-----|-------|
| 1-2 | SMTP email support |
| 3-4 | PDF generation (WeasyPrint) |
| 5-6 | S3 upload tool |
| 7-8 | GitHub connector (issues, PRs, comments) |
| 9-10 | Jira connector (issues, comments) |

**Deliverable:** Professional document output, dev tool integrations

---

### Sprint 3 (Week 5-6): Workflow Power
**Goal:** Agents can chain and schedule

| Day | Tasks |
|-----|-------|
| 1-2 | Scheduled actions (job queue) |
| 3-4 | Multi-agent invocation tool |
| 5-6 | Linear connector |
| 7-8 | Notion connector |
| 9-10 | Testing + polish |

**Deliverable:** Complex multi-step automations possible

---

### Sprint 4 (Week 7-10): OAuth & Enterprise
**Goal:** First-class integrations with major platforms

| Day | Tasks |
|-----|-------|
| 1-5 | Google OAuth infrastructure |
| 6-8 | Google Calendar connector |
| 9-10 | Google Sheets connector |
| 11-12 | Google Drive connector |
| 13-15 | Slack App OAuth |
| 16-18 | Human approval nodes |
| 19-20 | Testing + documentation |

**Deliverable:** Enterprise-ready, human-in-the-loop workflows

---

## 🎯 Success Metrics by Sprint

| Sprint | Scenarios Fully Covered | Key Capability |
|--------|------------------------|----------------|
| Sprint 1 | 2/6 (Briefing*, E-commerce*) | Notifications + State |
| Sprint 2 | 4/6 (+Developer, +Analyst*) | Documents + Dev tools |
| Sprint 3 | 5/6 (+Support*) | Chaining + Scheduling |
| Sprint 4 | 6/6 (All scenarios) | OAuth + Approvals |

*\* = Partially covered, core flow works*

---

## Implementation Order Cheat Sheet

```
Week 1:  🟢 Slack → Discord → Email API → SMS → KV Store
Week 2:  🟢 File Write → CSV → Audit → Rate Limit  
Week 3:  🟡 SMTP → PDF → S3
Week 4:  🟡 GitHub → Jira
Week 5:  🟡 Scheduled Actions → Multi-Agent
Week 6:  🟡 Linear → Notion
Week 7:  🔴 Google OAuth
Week 8:  🔴 Calendar → Sheets → Drive
Week 9:  🔴 Slack OAuth
Week 10: 🔴 Human Approval
```

---

## Quick Reference: What to Build

### New Tools (20 total)
```
Output:     send_slack, send_discord, send_email, send_sms
State:      kv_get, kv_set, kv_delete, kv_list
Files:      write_file, read_file, list_files, generate_csv, generate_pdf
Cloud:      upload_s3
Workflow:   schedule_action, invoke_agent
Connectors: github_*, jira_*, linear_*, notion_*
```

### New DB Tables (4 total)
```
agent_state       - KV store for agent memory
audit_logs        - Tool invocation history  
scheduled_actions - Future action queue
oauth_connectors  - OAuth token storage
```

### New Workflow Nodes (2 total)
```
approval_node  - Human-in-the-loop
delay_node     - Wait for duration/condition
```

