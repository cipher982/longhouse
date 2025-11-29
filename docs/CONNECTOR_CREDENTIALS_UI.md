# Connector Credentials UI - Design Document

> **Status**: Planning
> **Created**: November 2024
> **Author**: Claude & David Rose
> **Related**: `scenarios.md`, `implementation_roadmap.md`

---

## Table of Contents
1. [Executive Summary](#executive-summary)
2. [Background & Context](#background--context)
3. [Current State Analysis](#current-state-analysis)
4. [Problem Statement](#problem-statement)
5. [Design Goals](#design-goals)
6. [Proposed Solution](#proposed-solution)
7. [Technical Architecture](#technical-architecture)
8. [Implementation Plan](#implementation-plan)
9. [Open Questions](#open-questions)
10. [Appendix](#appendix)

---

## Executive Summary

### What This Is
A new UI component that allows users to configure credentials (API keys, webhooks, tokens) for built-in connector tools (Slack, Discord, Email, SMS, GitHub, Jira, Linear, Notion) in the Zerg agent platform.

### Why We Need It
We just implemented 8 connector tools that enable agents to send notifications and interact with project management systems. However, these tools currently require users to provide credentials (API keys, webhook URLs) as parameters every time they're called, which is:
- Inconvenient (copy-paste credentials repeatedly)
- Insecure (credentials visible in chat logs)
- Inconsistent (MCP servers have credential management, built-in tools don't)

### What Success Looks Like
Users can configure connector credentials once in a dedicated UI, and agents can reference those credentials by name when executing tools, similar to how environment variables work.

---

## Background & Context

### Project History

**Zerg Platform Overview**
- AI agent platform for automation and workflows
- Built on LangGraph + FastAPI backend, React frontend
- Supports custom agents with configurable tools
- Two tool types: Built-in tools (Python functions) and MCP servers (external services)

**Timeline of Connector Development**
1. **May 2024**: Initial platform with basic tools (`http_request`, `container_exec`, `get_current_time`)
2. **Oct 2024**: MCP (Model Context Protocol) integration added - allows connecting to external tool servers
3. **Nov 2024**: Gap analysis (`scenarios.md`) identified missing notification/output capabilities
4. **Nov 2024**: 8 connector tools implemented as built-in tools

### The 8 Connector Tools

**Notifications** (4 tools):
- `send_slack_webhook` - Send messages to Slack via webhook
- `send_discord_webhook` - Send messages to Discord with embeds
- `send_email` - Send email via Resend API
- `send_sms` - Send SMS via Twilio API

**Project Management** (4 tool sets, 24 tools total):
- **GitHub** (6 tools): create_issue, list_issues, get_issue, add_comment, list_pull_requests, get_pull_request
- **Jira** (6 tools): create_issue, list_issues, get_issue, add_comment, transition_issue, update_issue
- **Linear** (6 tools): create_issue, list_issues, get_issue, update_issue, add_comment, list_teams
- **Notion** (6 tools): create_page, get_page, update_page, search, query_database, append_blocks

**Implementation Status**:
- ✅ All 8 connectors implemented with full error handling
- ✅ 104 unit tests (all passing)
- ✅ Integration test infrastructure
- ✅ 7 of 8 connectors tested live (only Twilio SMS pending phone number purchase)
- ❌ No credential management UI

### Why Built-in vs MCP?

**Built-in Tools** (our 8 connectors):
- Simple, reliable, no external dependencies
- Direct Python implementation
- Predictable performance
- Easier to debug and maintain
- Ideal for common integrations everyone needs

**MCP Servers** (external tool providers):
- Flexible, extensible
- Community-contributed
- Good for niche/custom integrations
- Requires external server running
- More complex setup

**Decision**: Keep these 8 as built-in because they're universal needs, but we need UI parity with MCP credential management.

---

## Current State Analysis

### How Tool Configuration Works Today

#### Tool Selection (Works Well)
**Location**: Agent Settings Drawer → Tools tab

Users can:
1. See all available tools (built-in + MCP)
2. Enable/disable tools via checkbox list
3. Use wildcard patterns (e.g., `github_*` to enable all GitHub tools)
4. Auto-save with debounce (shows "●" saving indicator)

**Storage**: `agent.allowed_tools` (array of strings)
```json
{
  "allowed_tools": ["http_request", "send_slack_webhook", "github_*"]
}
```

#### MCP Server Credentials (Works Well)
**Location**: Agent Settings Drawer → MCP Servers section

**Features**:
- Add MCP server by preset (github, linear, slack, notion, asana) or custom URL
- Configure auth token (encrypted at rest)
- Test connection before saving
- Filter which tools from server to expose
- View server status (online/offline)
- See available tools list

**Example Flow**:
1. User clicks "Add MCP Server"
2. Selects preset "github" or enters custom URL
3. Pastes API token (type="password" input)
4. Clicks "Test connection" - validates token works
5. Optionally filters tools (e.g., only enable `create_issue`, not `delete_repository`)
6. Clicks "Save"
7. MCP tools appear as `mcp_github_create_issue` in tool list

**Backend Storage**:
```python
agent.config = {
  "mcp_servers": [
    {
      "preset": "github",
      "auth_token": "encrypted_ghp_abc123...",  # crypto.encrypt()
      "allowed_tools": ["create_issue", "search_issues"]
    }
  ]
}
```

**Security**: Auth tokens encrypted using `zerg.utils.crypto.encrypt()` (symmetric encryption)

#### Built-in Tool Credentials (Doesn't Exist)

**Current Situation**: No credential storage or UI

Users must provide credentials every time:
```python
# What users do today - pass creds as parameters
send_slack_webhook(
    webhook_url="https://hooks.slack.com/services/T.../B.../xxx",  # Paste every time
    text="Hello from agent"
)

github_create_issue(
    token="ghp_abc123...",  # Paste every time
    owner="myorg",
    repo="myrepo",
    title="Bug report"
)
```

**Problems**:
1. **Inconvenient**: Copy-paste credentials for every agent invocation
2. **Insecure**: Credentials visible in:
   - Agent chat logs (stored in database)
   - Agent execution history
   - WebSocket messages (if user inspects network)
3. **Error-prone**: Typos in pasted credentials cause failures
4. **No validation**: Can't test if credentials work until agent tries to use them
5. **Inconsistent UX**: MCP servers have nice credential UI, built-in tools don't

### User Experience Gap

**Scenario**: User wants agent to notify Slack when GitHub issue is created

**With MCP Servers** (hypothetical):
```
✅ Configure once in UI:
   1. Add GitHub MCP server → paste token → test → save
   2. Add Slack MCP server → paste webhook → test → save
   3. Agent can now use mcp_github_create_issue and mcp_slack_send_message

✅ Agent execution:
   "Create GitHub issue #123, then notify #engineering channel"
   → Uses stored credentials automatically
```

**With Built-in Tools** (current reality):
```
❌ Configure... nowhere:
   1. User must remember Slack webhook URL
   2. User must remember GitHub token
   3. No way to test credentials

❌ Agent execution:
   "Create GitHub issue using token ghp_abc123... and notify
    Slack at https://hooks.slack.com/services/T.../B.../xxx"
   → User must provide credentials in every message
   → Credentials logged in chat history
```

### Why Not Just Use MCP Servers?

**Considered Option**: Convert our 8 built-in connectors to MCP servers

**Why We Rejected This**:
1. **Architectural mismatch**: MCP servers are external processes, our tools are Python functions
2. **Complexity**: Would require running 8 separate MCP server processes
3. **Performance**: Network overhead for every tool call vs in-process function
4. **Reliability**: More moving parts = more failure points
5. **Maintenance**: Have to support MCP protocol changes
6. **User confusion**: "Why do I need to 'add a server' for built-in functionality?"

**Better approach**: Built-in tools with UI parity to MCP credential management

---

## Problem Statement

### Core Problem
**Users cannot securely configure and store credentials for built-in connector tools, forcing them to expose secrets in chat messages and provide credentials repeatedly.**

### User Impact

**Without Credential UI**:
- Sarah (VP of Marketing) wants morning briefing sent to Slack
  - ❌ Must paste Slack webhook URL in agent instructions every time
  - ❌ Webhook URL visible in chat logs forever
  - ❌ If webhook changes, must update all agents manually

**With Credential UI**:
- Sarah configures Slack webhook once in agent settings
  - ✅ Agent references credential by name: `@slack_webhook`
  - ✅ Credentials encrypted, not logged
  - ✅ Update webhook in one place, all agents use new value

### Technical Debt

**Current State Creates**:
1. **Security vulnerability**: Credentials stored in plaintext in chat logs
2. **Bad UX pattern**: Inconsistent with MCP server credential handling
3. **Scalability issue**: Can't add more connectors without solving this first
4. **Support burden**: Users will ask "why is this so hard?" and we have no good answer

---

## Design Goals

### Primary Goals

1. **Security**
   - Credentials encrypted at rest (same as MCP servers)
   - Never logged or transmitted in plaintext
   - Per-agent credential scoping (Agent A's Slack webhook ≠ Agent B's)

2. **Usability**
   - One-time configuration per connector
   - Clear visual indication of configured/not-configured state
   - Test connection before saving
   - Easy to update/rotate credentials

3. **Consistency**
   - UI/UX matches existing MCP server configuration
   - Same security model (encryption, storage location)
   - Same validation patterns (test before save)

4. **Maintainability**
   - Easy to add connector #9, #10, etc.
   - Minimal backend changes when adding new connectors
   - Clear separation between connector config and general settings

### Secondary Goals

5. **Discoverability**
   - Users understand which connectors are available
   - Clear documentation on where to get credentials
   - Helpful error messages when credentials missing/invalid

6. **Flexibility**
   - Support multiple credentials per connector type (e.g., "work Slack" vs "personal Slack")
   - Optional credential naming (auto-generate if not provided)
   - Share credentials across agents (future: organization-level credentials)

### Non-Goals (Explicitly Out of Scope)

- ❌ OAuth flows (GitHub OAuth, Google OAuth) - stick to API keys/tokens for v1
- ❌ Organization-wide credential sharing - keep per-agent for now
- ❌ Credential rotation automation - manual update only
- ❌ Audit logging of credential usage - focus on storage/retrieval
- ❌ Credential permissions/ACLs - all tools can use all configured credentials

---

## Proposed Solution

### High-Level Architecture

```
┌─────────────────────────────────────────────┐
│ Agent Settings UI                           │
├─────────────────────────────────────────────┤
│ [General] [Tools] [Connectors] [MCP Servers]│  ← New tab
│                                             │
│ ┌─ Connectors ────────────────────────────┐│
│ │                                          ││
│ │ 🔔 Notifications                         ││
│ │  ┌────────────────────────────────────┐ ││
│ │  │ Slack Webhook              [Edit]   │ ││
│ │  │ Status: ✓ Configured               │ ││
│ │  │ webhook: ...ck.com/serv...6Ag      │ ││
│ │  └────────────────────────────────────┘ ││
│ │  ┌────────────────────────────────────┐ ││
│ │  │ Discord Webhook         [Configure]│ ││
│ │  │ Status: ⚠ Not configured           │ ││
│ │  └────────────────────────────────────┘ ││
│ │                                          ││
│ │ 🔧 Project Management                   ││
│ │  [GitHub] [Jira] [Linear] [Notion]     ││
│ └──────────────────────────────────────────┘│
└─────────────────────────────────────────────┘
```

### Data Model

**Backend Storage** (in `agent.config`):
```python
# New top-level config key
agent.config = {
  "connector_credentials": {
    "slack_webhook": {
      "type": "slack_webhook",
      "encrypted_value": "encrypted_https://hooks.slack.com/...",
      "display_name": "Team Slack",  # Optional user-provided name
      "created_at": "2024-11-29T10:00:00Z",
      "last_tested": "2024-11-29T10:01:00Z",
      "test_status": "success"  # or "failed", "never_tested"
    },
    "github_token": {
      "type": "github_token",
      "encrypted_value": "encrypted_ghp_abc123...",
      "display_name": "Personal GitHub",
      "metadata": {
        "username": "drose-d",  # Discovered during test
        "scopes": ["repo", "issues"]  # Discovered during test
      },
      "created_at": "2024-11-29T10:05:00Z",
      "last_tested": "2024-11-29T10:06:00Z",
      "test_status": "success"
    }
  },

  # Existing config preserved
  "mcp_servers": [...],
  "allowed_tools": [...]
}
```

**Why This Structure**:
- Top-level key for easy discovery
- Each credential is an object (not just encrypted string) for metadata
- `type` field allows multiple credentials of same type (future: "work_slack", "personal_slack")
- `test_status` cached for UI display
- `metadata` for discovered info (e.g., GitHub username, Slack workspace)

### Frontend Component Architecture

**New React Components**:

```
components/agent-settings/
├── ConnectorCredentialsPanel.tsx     ← Main panel (new tab content)
├── ConnectorCard.tsx                  ← Individual connector display
├── ConnectorConfigModal.tsx           ← Modal for add/edit credential
└── ConnectorTestButton.tsx            ← Test connection button
```

**Component Hierarchy**:
```tsx
<AgentSettingsDrawer>
  <Tabs>
    <Tab label="General">...</Tab>
    <Tab label="Tools">...</Tab>
    <Tab label="Connectors">  {/* NEW */}
      <ConnectorCredentialsPanel agentId={agentId}>
        <section className="connector-category">
          <h3>🔔 Notifications</h3>
          <ConnectorCard
            type="slack_webhook"
            credential={slackCred}
            onConfigure={handleConfigure}
            onTest={handleTest}
            onRemove={handleRemove}
          />
          <ConnectorCard type="discord_webhook" />
          <ConnectorCard type="resend_email" />
          <ConnectorCard type="twilio_sms" />
        </section>

        <section className="connector-category">
          <h3>🔧 Project Management</h3>
          <ConnectorCard type="github_token" />
          <ConnectorCard type="jira_token" />
          <ConnectorCard type="linear_token" />
          <ConnectorCard type="notion_token" />
        </section>
      </ConnectorCredentialsPanel>
    </Tab>
    <Tab label="MCP Servers">...</Tab>
  </Tabs>
</AgentSettingsDrawer>
```

### Backend API Design

**New Endpoints**:

```python
# List all connector types and their status
GET /agents/{agent_id}/connectors
→ Response: {
  "connectors": [
    {
      "type": "slack_webhook",
      "category": "notifications",
      "name": "Slack Webhook",
      "description": "Send messages to Slack channels",
      "configured": true,
      "display_name": "Team Slack",
      "test_status": "success",
      "last_tested": "2024-11-29T10:01:00Z",
      "docs_url": "https://api.slack.com/messaging/webhooks"
    },
    {
      "type": "github_token",
      "category": "project_management",
      "name": "GitHub",
      "description": "Create issues, PRs, and comments",
      "configured": true,
      "display_name": "Personal GitHub",
      "test_status": "success",
      "metadata": {"username": "drose-d"},
      "docs_url": "https://github.com/settings/tokens"
    },
    {
      "type": "discord_webhook",
      "category": "notifications",
      "name": "Discord Webhook",
      "configured": false,
      "docs_url": "https://discord.com/developers/docs/resources/webhook"
    }
  ]
}

# Configure a connector
POST /agents/{agent_id}/connectors
Body: {
  "type": "slack_webhook",
  "value": "https://hooks.slack.com/services/...",
  "display_name": "Team Slack"  # Optional
}
→ Response: {
  "success": true,
  "credential_id": "slack_webhook",
  "test_status": "not_tested"
}

# Test a connector
POST /agents/{agent_id}/connectors/{credential_id}/test
→ Response: {
  "success": true,
  "test_status": "success",
  "message": "Successfully sent test message to Slack",
  "metadata": {  # Discovered during test
    "workspace": "myteam.slack.com",
    "channel": "#general"
  }
}

# Update a connector
PATCH /agents/{agent_id}/connectors/{credential_id}
Body: {
  "value": "new_token_here",  # Optional
  "display_name": "Updated Name"  # Optional
}

# Delete a connector
DELETE /agents/{agent_id}/connectors/{credential_id}
```

**Connector Type Registry** (backend):
```python
# Location: zerg/connectors/registry.py
CONNECTOR_TYPES = {
    "slack_webhook": {
        "category": "notifications",
        "name": "Slack Webhook",
        "description": "Send messages to Slack channels",
        "docs_url": "https://api.slack.com/messaging/webhooks",
        "credential_fields": [
            {
                "key": "webhook_url",
                "label": "Webhook URL",
                "type": "url",
                "placeholder": "https://hooks.slack.com/services/...",
                "required": True
            }
        ],
        "test_function": "zerg.connectors.tests.test_slack_webhook"
    },
    "github_token": {
        "category": "project_management",
        "name": "GitHub",
        "description": "Create issues, PRs, and comments",
        "docs_url": "https://github.com/settings/tokens",
        "credential_fields": [
            {
                "key": "token",
                "label": "Personal Access Token",
                "type": "password",
                "placeholder": "ghp_...",
                "required": True
            },
            {
                "key": "default_repo",
                "label": "Default Repository (optional)",
                "type": "text",
                "placeholder": "owner/repo",
                "required": False
            }
        ],
        "test_function": "zerg.connectors.tests.test_github_token"
    }
    # ... 6 more
}
```

### Tool Execution Changes

**Before** (current):
```python
def send_slack_webhook(
    webhook_url: str,  # User provides every time
    text: str,
    blocks: Optional[List[Dict]] = None
) -> Dict[str, Any]:
    """Send message to Slack."""
    response = httpx.post(webhook_url, json={"text": text, "blocks": blocks})
    return {"success": response.status_code == 200}
```

**After** (with credential resolution):
```python
def send_slack_webhook(
    text: str,
    blocks: Optional[List[Dict]] = None,
    webhook_url: Optional[str] = None  # Optional override
) -> Dict[str, Any]:
    """Send message to Slack.

    Uses configured Slack webhook from agent settings.
    Optionally override with webhook_url parameter.
    """
    # Resolve credential from agent context
    if webhook_url is None:
        webhook_url = get_agent_credential("slack_webhook")
        if not webhook_url:
            return {
                "success": False,
                "error": "Slack webhook not configured. Configure in agent settings."
            }

    response = httpx.post(webhook_url, json={"text": text, "blocks": blocks})
    return {"success": response.status_code == 200}
```

**Agent Context Injection**:
```python
# In agent execution (zerg_react_agent.py)
from zerg.connectors.resolver import get_credential_resolver

# When binding tools to LLM
resolver = get_credential_resolver(agent_id=agent.id)
tools_with_context = [
    tool.bind_credential_resolver(resolver)
    for tool in BUILTIN_TOOLS
]

llm.bind_tools(tools_with_context)
```

**Credential Resolver** (new utility):
```python
# Location: zerg/connectors/resolver.py
class CredentialResolver:
    def __init__(self, agent_id: int, db: Session):
        self.agent_id = agent_id
        self.db = db
        self._cache = {}  # Cache decrypted credentials

    def get(self, credential_type: str) -> Optional[str]:
        """Get decrypted credential value."""
        if credential_type in self._cache:
            return self._cache[credential_type]

        agent = self.db.query(Agent).filter_by(id=self.agent_id).first()
        creds = agent.config.get("connector_credentials", {})

        if credential_type not in creds:
            return None

        encrypted = creds[credential_type]["encrypted_value"]
        decrypted = crypto.decrypt(encrypted)
        self._cache[credential_type] = decrypted
        return decrypted
```

---

## Technical Architecture

### Backend Components

**New Files**:
```
apps/zerg/backend/zerg/
├── connectors/
│   ├── __init__.py
│   ├── registry.py              ← CONNECTOR_TYPES definition
│   ├── resolver.py              ← CredentialResolver class
│   ├── validators.py            ← Credential validation logic
│   └── tests/
│       ├── __init__.py
│       ├── test_slack.py        ← Test Slack webhook
│       ├── test_github.py       ← Test GitHub token
│       └── ... (8 test modules)
├── routers/
│   └── connectors.py            ← New API endpoints
└── schemas/
    └── connector_schemas.py     ← Pydantic models
```

**Modified Files**:
```
apps/zerg/backend/zerg/
├── agents_def/
│   └── zerg_react_agent.py      ← Inject CredentialResolver
├── tools/builtin/
│   ├── slack_tools.py           ← Add credential resolution
│   ├── discord_tools.py         ← Add credential resolution
│   ├── email_tools.py           ← Add credential resolution
│   ├── sms_tools.py             ← Add credential resolution
│   ├── github_tools.py          ← Add credential resolution
│   ├── jira_tools.py            ← Add credential resolution
│   ├── linear_tools.py          ← Add credential resolution
│   └── notion_tools.py          ← Add credential resolution
```

### Frontend Components

**New Files**:
```
apps/zerg/frontend-web/src/
├── components/agent-settings/
│   ├── ConnectorCredentialsPanel.tsx    ← Main panel
│   ├── ConnectorCard.tsx                ← Connector display
│   ├── ConnectorConfigModal.tsx         ← Config modal
│   └── ConnectorTestButton.tsx          ← Test button
├── hooks/
│   ├── useConnectors.ts                 ← API hooks
│   └── useConnectorConfig.ts            ← Config state
└── types/
    └── connector-types.ts               ← TypeScript types
```

**Modified Files**:
```
apps/zerg/frontend-web/src/
├── components/agent-settings/
│   └── AgentSettingsDrawer.tsx          ← Add Connectors tab
├── api/
│   └── generated-api.ts                 ← Re-generate from OpenAPI
```

### Data Flow

**Configuration Flow**:
```
1. User opens Agent Settings → Connectors tab
   └→ GET /agents/{id}/connectors
      └→ Retrieve agent.config.connector_credentials
      └→ Return list with decrypted display info (not values)

2. User clicks "Configure" on Slack card
   └→ Modal opens with form (webhook URL input)

3. User pastes webhook URL, clicks "Test"
   └→ POST /agents/{id}/connectors/test (transient, not saved)
      └→ Validate URL format
      └→ Call test_slack_webhook(url)
      └→ Return success/failure + metadata

4. Test succeeds, user clicks "Save"
   └→ POST /agents/{id}/connectors
      └→ Encrypt credential with crypto.encrypt()
      └→ Store in agent.config.connector_credentials
      └→ Return success

5. UI updates to show "✓ Configured"
```

**Tool Execution Flow**:
```
1. User sends message: "Notify #engineering about deployment"
   └→ Agent decides to use send_slack_webhook tool

2. LangGraph calls tool function
   └→ send_slack_webhook(text="Deployment complete")
   └→ Tool checks: webhook_url parameter provided?
      └→ No → Resolve from agent context
         └→ resolver.get("slack_webhook")
            └→ Fetch agent.config.connector_credentials["slack_webhook"]
            └→ Decrypt encrypted_value
            └→ Return decrypted URL
      └→ Yes → Use provided URL (override)

3. Tool executes with resolved credential
   └→ httpx.post(webhook_url, json={...})

4. Result streamed back to user
```

### Security Considerations

**Encryption**:
- Same as MCP servers: `zerg.utils.crypto.encrypt()` / `decrypt()`
- Symmetric encryption (Fernet or similar)
- Key stored in environment variable (not in database)

**Access Control**:
- Credentials scoped to agent (agent_id)
- Only agent owner can view/edit credentials
- Credentials never sent to frontend (only encrypted)
- Test endpoint validates but doesn't return decrypted value

**Audit Trail** (future):
- Log when credentials added/updated/deleted
- Log test connection attempts
- DO NOT log actual credential values

**Threat Model**:
- ✅ Protected against: DB compromise (credentials encrypted)
- ✅ Protected against: Chat log exposure (credentials not in messages)
- ✅ Protected against: Network sniffing (HTTPS)
- ⚠️ Vulnerable to: Server compromise (if encryption key stolen)
- ⚠️ Vulnerable to: Admin access (admins can decrypt)

---

## Implementation Plan

### Phase 1: Backend Foundation (Week 1)

**Day 1-2: Core Infrastructure**
- [ ] Create `connectors/` module structure
- [ ] Implement `CredentialResolver` class
- [ ] Add encryption/decryption helpers (reuse MCP logic)
- [ ] Define `CONNECTOR_TYPES` registry

**Day 3-4: API Endpoints**
- [ ] `GET /agents/{id}/connectors` - List connectors
- [ ] `POST /agents/{id}/connectors` - Add/update credential
- [ ] `POST /agents/{id}/connectors/{id}/test` - Test connection
- [ ] `DELETE /agents/{id}/connectors/{id}` - Remove credential
- [ ] Update Pydantic schemas

**Day 5: Test Functions**
- [ ] Implement 8 test functions (one per connector)
- [ ] Test Slack webhook: Send test message
- [ ] Test GitHub token: Validate token, get user info
- [ ] Test Jira token: Validate domain + token
- [ ] Test Linear token: Query teams
- [ ] Test Notion token: Search workspace
- [ ] Test Discord webhook: Send test embed
- [ ] Test Resend API: Validate key (don't send email)
- [ ] Test Twilio API: Validate credentials (don't send SMS)

### Phase 2: Tool Updates (Week 1)

**Day 6-7: Update Tool Implementations**
- [ ] Modify 8 connector tool files to support credential resolution
- [ ] Add `Optional[str]` parameters for credential override
- [ ] Inject `CredentialResolver` into tool context
- [ ] Update tool schemas (LangChain StructuredTool)
- [ ] Add helpful error messages when credentials missing

**Testing**:
- [ ] Unit tests for credential resolution
- [ ] Integration tests for each connector with mocked credentials
- [ ] E2E test: Configure credential → use tool → verify success

### Phase 3: Frontend UI (Week 2)

**Day 8-10: React Components**
- [ ] `ConnectorCredentialsPanel.tsx` - Main panel with categories
- [ ] `ConnectorCard.tsx` - Individual connector display
- [ ] `ConnectorConfigModal.tsx` - Configuration modal
- [ ] `ConnectorTestButton.tsx` - Test connection button
- [ ] Add new tab to `AgentSettingsDrawer`

**Day 11: API Integration**
- [ ] Create `useConnectors` hook for API calls
- [ ] Create `useConnectorConfig` hook for state management
- [ ] Implement optimistic updates
- [ ] Add loading states and error handling

**Day 12: Styling & Polish**
- [ ] Match existing UI patterns (colors, spacing, shadows)
- [ ] Add icons for each connector type
- [ ] Implement status indicators (✓ Configured, ⚠ Not configured, ❌ Failed)
- [ ] Add tooltips and help text
- [ ] Responsive design for mobile

### Phase 4: Testing & Documentation (Week 2)

**Day 13: Testing**
- [ ] Frontend unit tests (React Testing Library)
- [ ] Backend unit tests for new endpoints
- [ ] E2E tests (Playwright) for full configuration flow
- [ ] Manual QA on all 8 connectors

**Day 14: Documentation & Launch**
- [ ] Update user docs with connector configuration guide
- [ ] Add "Where to get credentials" links to UI
- [ ] Create migration guide (if users have credentials elsewhere)
- [ ] Write changelog entry
- [ ] Deploy to staging → production

### Rollout Plan

**Beta Testing**:
1. Deploy to staging environment
2. Invite 5-10 beta users to configure connectors
3. Collect feedback on UX
4. Fix bugs and iterate

**Production Launch**:
1. Deploy to production
2. Announcement: "New Connector Credentials UI"
3. Monitor for errors (Sentry, logs)
4. Support users migrating from manual credential passing

**Success Metrics**:
- 80% of users configure at least 1 connector within first week
- <5% error rate on credential tests
- Zero security incidents related to credential exposure
- Positive user feedback on ease of use

---

## Open Questions

### Technical Decisions

**Q1: Credential Naming**
- Should we support multiple credentials per type? (e.g., "work_slack", "personal_slack")
- **Proposal**: Start with one credential per type, add naming in v2 if users request it
- **Rationale**: Simpler UI, covers 90% of use cases

**Q2: Credential Sharing**
- Should credentials be per-agent or organization-wide?
- **Proposal**: Per-agent for v1, organization-wide in v2
- **Rationale**: Security (least privilege), easier to implement

**Q3: OAuth Support**
- Should we support OAuth flows (GitHub OAuth, Google OAuth)?
- **Proposal**: Not for v1, stick to API keys/tokens
- **Rationale**: OAuth requires callback URLs, more complex UI, limited user demand

**Q4: Credential Rotation**
- Should we support automatic credential rotation?
- **Proposal**: Manual update only for v1
- **Rationale**: Complex feature, limited security benefit (most APIs don't auto-rotate)

**Q5: Backward Compatibility**
- Should we maintain support for passing credentials as parameters?
- **Proposal**: Yes, keep optional override parameter
- **Rationale**: Power users may want per-call override, useful for testing

### UX Decisions

**Q6: Credential Visibility**
- Should users see encrypted credentials in UI? (e.g., "ghp_abc...xyz")
- **Proposal**: Show last 4 characters only (e.g., "•••xyz")
- **Rationale**: Balance between visibility and security

**Q7: Test Connection UI**
- Should test be required before save, or optional?
- **Proposal**: Optional but strongly encouraged (prominent button)
- **Rationale**: Some users may want to save and test later

**Q8: Error Handling**
- How should we surface credential errors during agent execution?
- **Proposal**: Clear error message in tool output: "Slack webhook not configured. Configure in agent settings → Connectors."
- **Rationale**: Actionable error message guides user to fix

**Q9: Onboarding**
- Should we show a guided tour for first-time connector users?
- **Proposal**: Simple tooltip on first visit to Connectors tab
- **Rationale**: Lightweight, not intrusive

**Q10: Mobile Experience**
- Should connector configuration work on mobile?
- **Proposal**: Yes, but test-focused (configuration likely desktop)
- **Rationale**: Users may monitor agents on mobile, configure on desktop

---

## Appendix

### A. Connector Type Details

| Connector | Credential Type | Validation Method | Test Function |
|-----------|----------------|-------------------|---------------|
| Slack | Webhook URL | Format check (https://hooks.slack.com/...) | Send test message |
| Discord | Webhook URL | Format check (https://discord.com/api/webhooks/...) | Send test embed |
| Resend | API Key | Format check (re_...) | GET /domains (list domains) |
| Twilio | Account SID + Auth Token | Format check (AC... + alphanumeric) | GET /Accounts/{sid} |
| GitHub | Personal Access Token | Format check (ghp_... or github_pat_...) | GET /user (get authenticated user) |
| Jira | Domain + Email + API Token | Format check (*.atlassian.net) | GET /rest/api/3/myself |
| Linear | API Key | Format check (lin_api_...) | Query teams (GraphQL) |
| Notion | Integration Token | Format check (secret_... or ntn_...) | Search workspace (empty query) |

### B. Example User Flows

**Flow 1: First-time Setup**
```
1. User creates new agent "Deploy Notifier"
2. Opens Agent Settings → Connectors tab (sees empty state)
3. Card shows "Slack Webhook - ⚠ Not configured"
4. Clicks "Configure"
5. Modal opens: "Where to get webhook URL?" link → Slack API docs
6. User creates webhook in Slack settings, copies URL
7. Pastes URL in modal
8. Clicks "Test connection"
9. Success message: "✓ Test message sent to #general"
10. Clicks "Save"
11. Card updates: "Slack Webhook - ✓ Configured"
12. User starts chatting with agent: "Notify team about deployment"
13. Agent uses send_slack_webhook (credential auto-resolved)
14. Message appears in Slack
```

**Flow 2: Credential Update**
```
1. User's GitHub token expires
2. Agent execution fails: "GitHub authentication failed"
3. User opens Agent Settings → Connectors
4. Card shows "GitHub - ❌ Last test failed"
5. Clicks "Edit"
6. Updates token, clicks "Test"
7. Success: "✓ Connected as drose-d"
8. Clicks "Save"
9. Agent can now use GitHub tools again
```

**Flow 3: Multi-Agent Setup**
```
1. User has 3 agents: "Deploy", "Monitor", "Report"
2. Configures Slack webhook on "Deploy" agent
3. Wants same webhook for "Monitor" agent
4. Opens "Monitor" settings → Connectors
5. Configures Slack (pastes same webhook)
6. (Future: Could have "Copy from Deploy agent" option)
```

### C. Comparison with MCP Servers

| Aspect | MCP Servers | Connector Credentials |
|--------|-------------|----------------------|
| **Tools** | External (mcp_github_*) | Built-in (github_*) |
| **Setup** | Add server + auth | Configure credential |
| **Storage** | agent.config.mcp_servers | agent.config.connector_credentials |
| **Encryption** | ✅ Yes | ✅ Yes (same method) |
| **Test UI** | ✅ Yes | ✅ Yes (same pattern) |
| **Scope** | Per-agent | Per-agent |
| **Flexibility** | High (custom servers) | Low (fixed 8 connectors) |
| **Complexity** | High (external process) | Low (in-process function) |
| **Performance** | Network overhead | Direct function call |

### D. Security Checklist

**Storage**:
- [x] Credentials encrypted at rest
- [x] Encryption key not in database
- [x] Credentials never in plaintext logs
- [x] Credentials never sent to frontend
- [ ] Audit log for credential changes (future)

**Transmission**:
- [x] HTTPS only (enforced)
- [x] Credentials in request body (not URL)
- [x] No credentials in WebSocket messages
- [ ] Rate limiting on test endpoint (future)

**Access Control**:
- [ ] Only agent owner can configure credentials
- [ ] Credentials scoped to agent (not global)
- [ ] No cross-agent credential access
- [ ] Admin role can view but not decrypt (future)

**Error Handling**:
- [x] Never leak credentials in error messages
- [x] Generic errors to user ("Authentication failed")
- [x] Detailed errors to logs (sanitized)
- [ ] Alert on repeated failed tests (future)

### E. Related Documentation

- `scenarios.md` - Original use cases driving connector development
- `implementation_roadmap.md` - Connector implementation status
- `docs/EMAIL_TOOL.md` - Resend email connector docs
- `docs/JIRA_CONNECTOR.md` - Jira connector docs
- `docs/LINEAR_CONNECTOR.md` - Linear connector docs
- `docs/sms_twilio_integration.md` - Twilio SMS docs
- `apps/zerg/backend/zerg/routers/mcp_servers.py` - MCP server API (similar pattern)
- `apps/zerg/frontend-web/src/components/agent-settings/AgentSettingsDrawer.tsx` - Where to add UI

### F. Future Enhancements (Post-v1)

**P1 - High Value**:
1. Organization-wide credential sharing (team Slack webhook)
2. Multiple credentials per type ("work GitHub", "personal GitHub")
3. OAuth support (GitHub OAuth, Google OAuth)
4. Credential expiry notifications
5. Usage analytics (which credentials used most)

**P2 - Nice to Have**:
6. Credential import/export (for backup/migration)
7. Credential templates (pre-fill based on connector type)
8. Bulk credential testing (test all at once)
9. Credential health dashboard
10. Auto-rotation for supported services

**P3 - Advanced**:
11. Secrets manager integration (HashiCorp Vault, AWS Secrets Manager)
12. Credential permissions/ACLs (read-only tokens)
13. Just-in-time credential provisioning
14. Credential sharing marketplace (pre-configured webhooks)
15. Compliance reporting (SOC2, HIPAA)

---

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2024-11-29 | Claude & David Rose | Initial draft |

---

**Next Steps**: Review this document, gather feedback, and begin Phase 1 implementation when ready to proceed.
