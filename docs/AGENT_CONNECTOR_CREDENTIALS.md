# Agent Connector Credentials

> **Status**: Ready for Implementation  
> **Created**: November 2024  
> **Author**: David Rose & Claude  
> **Supersedes**: `docs/CONNECTOR_CREDENTIALS_UI.md` (original research)

---

## Table of Contents

1. [Executive Summary](#executive-summary)
2. [Background](#background)
3. [Problem Statement](#problem-statement)
4. [Design Decision: Per-Agent Scope](#design-decision-per-agent-scope)
5. [Solution Overview](#solution-overview)
6. [Technical Specification](#technical-specification)
7. [Implementation Plan](#implementation-plan)
8. [Definition of Done](#definition-of-done)

---

## Executive Summary

### What We're Building

A credential management system that allows users to configure API keys, tokens, and webhooks for built-in connector tools (Slack, Discord, GitHub, etc.) on a **per-agent basis**. Credentials are stored encrypted in a dedicated database table and automatically injected into tools at execution time.

### Why It Matters

Currently, our 8 connector tools require users to paste credentials directly into chat messages every time they want an agent to send a Slack notification or create a GitHub issue. This is:

- **Insecure**: Credentials are logged in chat history
- **Tedious**: Copy-paste the same webhook URL repeatedly  
- **Inconsistent**: MCP servers have credential management; built-in tools don't

### End Result

Users configure credentials once in the Agent Settings UI. Agents use those credentials automatically. No secrets in chat logs. No repetitive configuration.

---

## Background

### The Zerg Platform

Zerg is an AI agent platform for automation and workflows. Key characteristics:

- **Backend**: FastAPI + LangGraph (Python)
- **Frontend**: React + TypeScript + Vite
- **Database**: PostgreSQL (prod) / SQLite (dev)
- **Deployment**: Coolify on dedicated server at swarmlet.com

### Project Stage

- Single developer (David Rose)
- No paying users yet (beta)
- Deployed to production for learning and iteration
- Goal: Get first paying customer

### Tool Architecture

Zerg agents have access to two types of tools:

1. **Built-in Tools**: Python functions bundled with the platform
   - `http_request`, `get_current_time`, `container_exec`
   - The 8 connector tools (Slack, Discord, Email, SMS, GitHub, Jira, Linear, Notion)

2. **MCP Servers**: External tool providers via Model Context Protocol
   - User configures server URL + auth token
   - Platform connects and exposes tools as `mcp_<server>_<tool>`

### The 8 Connector Tools

Implemented in November 2024 to address gaps in agent output capabilities:

| Category | Connector | Tools | Status |
|----------|-----------|-------|--------|
| **Notifications** | Slack | `send_slack_webhook` | ✅ Tested |
| | Discord | `send_discord_webhook` | ✅ Tested |
| | Email (Resend) | `send_email` | ✅ Tested |
| | SMS (Twilio) | `send_sms` | ⚠️ Needs phone number |
| **Project Mgmt** | GitHub | 6 tools (issues, PRs, comments) | ✅ Tested |
| | Jira | 6 tools (issues, transitions) | ✅ Tested |
| | Linear | 6 tools (issues, teams) | ✅ Tested |
| | Notion | 6 tools (pages, databases) | ✅ Tested |

**Total**: 28 tools across 8 connectors, with 104 unit tests passing.

### Existing Infrastructure

The codebase already has relevant patterns we'll reuse:

1. **`Connector` model** (`models.py` lines 105-138): User-scoped connector for Gmail OAuth. We're creating a separate agent-scoped model for tool credentials.

2. **`crypto.py`**: Fernet encryption for secrets. Already used for Gmail refresh tokens.

3. **Alembic**: Migration infrastructure in place with 8 existing migrations.

4. **Agent Settings Drawer**: React component with tabs (General, Tools, MCP Servers). We'll add a "Connectors" tab.

---

## Problem Statement

### The Core Issue

**Built-in connector tools require credentials as function parameters, forcing users to expose secrets in chat messages.**

### Current User Experience

```
User: Send a message to Slack saying "Deploy complete"

Agent: I need the Slack webhook URL to send the message.

User: Use https://hooks.slack.com/services/T05XXXXX/B07XXXXX/xxxxxxxxxxx

Agent: ✓ Message sent to Slack
```

**Problems with this flow:**

1. **Security Risk**: The webhook URL is now stored in:
   - Thread messages table (plaintext)
   - WebSocket message history
   - Any logging that captures chat

2. **Poor UX**: User must remember and paste the URL every session

3. **Error Prone**: Typos in pasted credentials cause cryptic failures

4. **Inconsistent**: MCP servers have a nice credential UI; built-in tools don't

### Impact

- Users avoid using connector tools due to friction
- Security-conscious users can't use the platform for sensitive integrations
- Support burden when credentials get logged and need rotation

---

## Design Decision: Per-Agent Scope

### The Question

Should connector credentials be scoped to **users** or **agents**?

| Scope | Description |
|-------|-------------|
| **Per-User** | Configure once, all agents owned by that user share it |
| **Per-Agent** | Configure per agent, each agent has its own credentials |

### Analysis

**Per-User Pros:**
- Less repetitive (configure GitHub token once)
- Matches the existing `Connector` model pattern

**Per-User Cons:**
- Violates least privilege (all agents see all secrets)
- Can't have different Slack channels for different agents
- Harder to audit which agent used which credential
- Complicates future team/org features

**Per-Agent Pros:**
- Least privilege by default
- Matches mental model (configure agent = configure everything it needs)
- Clean audit trail (agent X used credential Y)
- Easy path to team features (agent ownership already exists)

**Per-Agent Cons:**
- Must paste same credential multiple times for multiple agents
- More rows in database

### Decision: Per-Agent

For an indie SaaS targeting solo developers and small teams:

1. **Security wins** – Credentials scoped to exactly what needs them
2. **Mental model wins** – "This agent talks to this Slack channel"
3. **Future-proof** – When teams arrive, no retrofit needed
4. **Acceptable trade-off** – Pasting a credential twice is fine at this scale

---

## Solution Overview

### User Experience: Configuration

**Agent Settings Drawer → Connectors Tab**

```
┌─────────────────────────────────────────────────────────────┐
│  Agent Settings: Deploy Bot                                 │
├─────────────────────────────────────────────────────────────┤
│  [General]  [Tools]  [Connectors]  [MCP Servers]            │
│                                                             │
│  ┌───────────────────────────────────────────────────────┐  │
│  │  🔔 NOTIFICATIONS                                     │  │
│  │                                                       │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  Slack                           ✓ Connected    │  │  │
│  │  │  Webhook: ••••••••••••x8Kq                      │  │  │
│  │  │  Last tested: 2 hours ago                       │  │  │
│  │  │  [Test] [Edit] [Remove]                         │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  │                                                       │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  Discord                         Not configured  │  │  │
│  │  │  Send messages to Discord channels              │  │  │
│  │  │  [Configure]                                    │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  │                                                       │  │
│  │  🔧 PROJECT MANAGEMENT                                │  │
│  │                                                       │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  GitHub                          ✓ Connected    │  │  │
│  │  │  Token: ••••••••••••x7Kq                        │  │  │
│  │  │  User: drose-d • Scopes: repo, issues           │  │  │
│  │  │  [Test] [Edit] [Remove]                         │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  │                                                       │  │
│  │  ┌─────────────────────────────────────────────────┐  │  │
│  │  │  Jira                            Not configured  │  │  │
│  │  │  Create and manage Jira issues                  │  │  │
│  │  │  [Configure]                                    │  │  │
│  │  └─────────────────────────────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
```

### User Experience: Configuration Modal

```
┌─────────────────────────────────────────────────────────────┐
│  Configure Slack                                       [X]  │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Webhook URL *                                              │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ https://hooks.slack.com/services/...                │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│  ℹ️ Get your webhook URL from Slack:                        │
│     1. Go to api.slack.com/apps                            │
│     2. Create or select an app                             │
│     3. Enable Incoming Webhooks                            │
│     4. Add webhook to your channel                         │
│                                                             │
│  Display Name (optional)                                    │
│  ┌─────────────────────────────────────────────────────┐   │
│  │ #engineering channel                                │   │
│  └─────────────────────────────────────────────────────┘   │
│                                                             │
│                              [Test Connection]  [Save]      │
└─────────────────────────────────────────────────────────────┘
```

### User Experience: Agent Execution

```
User: Create a GitHub issue for the login bug and notify #engineering

Agent: I'll create the issue and notify the team.

    📋 Creating GitHub issue...
    ✓ Created issue #142: "Login bug on mobile"
    
    💬 Sending Slack notification...
    ✓ Notified #engineering channel

Done! The issue has been created and the team has been notified.
```

**Key points:**
- No credentials in the message
- No prompting user for credentials
- Clean execution flow

### User Experience: Missing Credential

```
User: Send a message to Discord about the deployment

Agent: I can't send Discord messages because Discord isn't configured 
for this agent.

To fix this:
1. Open Agent Settings
2. Go to the Connectors tab
3. Configure Discord with your webhook URL

Would you like me to help you find where to get a Discord webhook URL?
```

---

## Technical Specification

### Database Schema

**New Table: `connector_credentials`**

```sql
CREATE TABLE connector_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    
    -- Foreign key to agent (CASCADE delete)
    agent_id INTEGER NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    
    -- Connector type identifier
    -- Values: 'slack', 'discord', 'email', 'sms', 'github', 'jira', 'linear', 'notion'
    connector_type VARCHAR(50) NOT NULL,
    
    -- Encrypted credential value (Fernet AES-GCM)
    -- For simple connectors: the raw credential (webhook URL, API token)
    -- For complex connectors: JSON with multiple fields
    encrypted_value TEXT NOT NULL,
    
    -- Optional user-friendly label
    display_name VARCHAR(255),
    
    -- Metadata discovered during test (e.g., GitHub username, Slack workspace)
    -- Stored as JSON, NOT encrypted (no secrets here)
    metadata JSON,
    
    -- Test status tracking
    test_status VARCHAR(20) NOT NULL DEFAULT 'untested',  -- 'untested', 'success', 'failed'
    last_tested_at TIMESTAMP,
    
    -- Timestamps
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    
    -- Constraints
    UNIQUE(agent_id, connector_type)  -- One credential per type per agent
);

CREATE INDEX ix_connector_credentials_agent_id ON connector_credentials(agent_id);
```

### SQLAlchemy Model

```python
# apps/zerg/backend/zerg/models/models.py

class ConnectorCredential(Base):
    """Encrypted credential for a built-in connector tool.
    
    Scoped to a single agent. Each agent can have at most one credential
    per connector type (e.g., one Slack webhook, one GitHub token).
    """
    
    __tablename__ = "connector_credentials"
    __table_args__ = (
        UniqueConstraint("agent_id", "connector_type", name="uix_agent_connector"),
    )
    
    id = Column(Integer, primary_key=True, index=True)
    agent_id = Column(Integer, ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True)
    connector_type = Column(String(50), nullable=False)
    encrypted_value = Column(Text, nullable=False)
    display_name = Column(String(255), nullable=True)
    metadata = Column(MutableDict.as_mutable(JSON), nullable=True)
    test_status = Column(String(20), nullable=False, default="untested")
    last_tested_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now(), nullable=False)
    
    # Relationships
    agent = relationship("Agent", backref="connector_credentials")
```

### Connector Type Registry

```python
# apps/zerg/backend/zerg/connectors/registry.py

from enum import Enum
from typing import TypedDict, List

class ConnectorType(str, Enum):
    SLACK = "slack"
    DISCORD = "discord"
    EMAIL = "email"
    SMS = "sms"
    GITHUB = "github"
    JIRA = "jira"
    LINEAR = "linear"
    NOTION = "notion"

class CredentialField(TypedDict):
    key: str
    label: str
    type: str  # 'text', 'password', 'url'
    placeholder: str
    required: bool

class ConnectorDefinition(TypedDict):
    type: ConnectorType
    name: str
    description: str
    category: str  # 'notifications' or 'project_management'
    icon: str  # Emoji or icon name
    docs_url: str
    fields: List[CredentialField]

CONNECTOR_REGISTRY: dict[ConnectorType, ConnectorDefinition] = {
    ConnectorType.SLACK: {
        "type": ConnectorType.SLACK,
        "name": "Slack",
        "description": "Send messages to Slack channels via webhook",
        "category": "notifications",
        "icon": "💬",
        "docs_url": "https://api.slack.com/messaging/webhooks",
        "fields": [
            {
                "key": "webhook_url",
                "label": "Webhook URL",
                "type": "url",
                "placeholder": "https://hooks.slack.com/services/...",
                "required": True,
            }
        ],
    },
    ConnectorType.DISCORD: {
        "type": ConnectorType.DISCORD,
        "name": "Discord",
        "description": "Send messages to Discord channels via webhook",
        "category": "notifications",
        "icon": "🎮",
        "docs_url": "https://discord.com/developers/docs/resources/webhook",
        "fields": [
            {
                "key": "webhook_url",
                "label": "Webhook URL",
                "type": "url",
                "placeholder": "https://discord.com/api/webhooks/...",
                "required": True,
            }
        ],
    },
    ConnectorType.EMAIL: {
        "type": ConnectorType.EMAIL,
        "name": "Email (Resend)",
        "description": "Send emails via Resend API",
        "category": "notifications",
        "icon": "📧",
        "docs_url": "https://resend.com/docs/api-reference/api-keys",
        "fields": [
            {
                "key": "api_key",
                "label": "API Key",
                "type": "password",
                "placeholder": "re_...",
                "required": True,
            },
            {
                "key": "from_email",
                "label": "From Email",
                "type": "text",
                "placeholder": "noreply@yourdomain.com",
                "required": True,
            },
        ],
    },
    ConnectorType.SMS: {
        "type": ConnectorType.SMS,
        "name": "SMS (Twilio)",
        "description": "Send SMS messages via Twilio",
        "category": "notifications",
        "icon": "📱",
        "docs_url": "https://www.twilio.com/docs/usage/api",
        "fields": [
            {
                "key": "account_sid",
                "label": "Account SID",
                "type": "text",
                "placeholder": "AC...",
                "required": True,
            },
            {
                "key": "auth_token",
                "label": "Auth Token",
                "type": "password",
                "placeholder": "",
                "required": True,
            },
            {
                "key": "from_number",
                "label": "From Phone Number",
                "type": "text",
                "placeholder": "+1234567890",
                "required": True,
            },
        ],
    },
    ConnectorType.GITHUB: {
        "type": ConnectorType.GITHUB,
        "name": "GitHub",
        "description": "Create issues, PRs, and comments on GitHub",
        "category": "project_management",
        "icon": "🐙",
        "docs_url": "https://github.com/settings/tokens",
        "fields": [
            {
                "key": "token",
                "label": "Personal Access Token",
                "type": "password",
                "placeholder": "ghp_... or github_pat_...",
                "required": True,
            }
        ],
    },
    ConnectorType.JIRA: {
        "type": ConnectorType.JIRA,
        "name": "Jira",
        "description": "Create and manage Jira issues",
        "category": "project_management",
        "icon": "🎫",
        "docs_url": "https://id.atlassian.com/manage-profile/security/api-tokens",
        "fields": [
            {
                "key": "domain",
                "label": "Jira Domain",
                "type": "text",
                "placeholder": "yourcompany.atlassian.net",
                "required": True,
            },
            {
                "key": "email",
                "label": "Email",
                "type": "text",
                "placeholder": "you@company.com",
                "required": True,
            },
            {
                "key": "api_token",
                "label": "API Token",
                "type": "password",
                "placeholder": "",
                "required": True,
            },
        ],
    },
    ConnectorType.LINEAR: {
        "type": ConnectorType.LINEAR,
        "name": "Linear",
        "description": "Create and manage Linear issues",
        "category": "project_management",
        "icon": "📐",
        "docs_url": "https://linear.app/settings/api",
        "fields": [
            {
                "key": "api_key",
                "label": "API Key",
                "type": "password",
                "placeholder": "lin_api_...",
                "required": True,
            }
        ],
    },
    ConnectorType.NOTION: {
        "type": ConnectorType.NOTION,
        "name": "Notion",
        "description": "Create and manage Notion pages and databases",
        "category": "project_management",
        "icon": "📝",
        "docs_url": "https://www.notion.so/my-integrations",
        "fields": [
            {
                "key": "api_key",
                "label": "Integration Token",
                "type": "password",
                "placeholder": "secret_... or ntn_...",
                "required": True,
            }
        ],
    },
}
```

### Credential Resolver

```python
# apps/zerg/backend/zerg/connectors/resolver.py

import json
from typing import Any, Optional
from sqlalchemy.orm import Session

from zerg.models.models import ConnectorCredential
from zerg.utils.crypto import decrypt
from zerg.connectors.registry import ConnectorType

class CredentialResolver:
    """Resolves and decrypts credentials for an agent's connector tools.
    
    Instantiated per-request with the agent's ID. Caches decrypted values
    for the lifetime of the request to avoid repeated DB queries.
    """
    
    def __init__(self, agent_id: int, db: Session):
        self.agent_id = agent_id
        self.db = db
        self._cache: dict[str, Any] = {}
    
    def get(self, connector_type: ConnectorType | str) -> Optional[dict[str, Any]]:
        """Get decrypted credential for a connector type.
        
        Returns:
            dict with credential fields, or None if not configured.
            For single-field connectors (Slack), returns {"webhook_url": "..."}.
            For multi-field connectors (Jira), returns {"domain": "...", "email": "...", "api_token": "..."}.
        """
        type_str = connector_type.value if isinstance(connector_type, ConnectorType) else connector_type
        
        if type_str in self._cache:
            return self._cache[type_str]
        
        cred = (
            self.db.query(ConnectorCredential)
            .filter(
                ConnectorCredential.agent_id == self.agent_id,
                ConnectorCredential.connector_type == type_str,
            )
            .first()
        )
        
        if not cred:
            self._cache[type_str] = None
            return None
        
        try:
            decrypted = decrypt(cred.encrypted_value)
            # Stored as JSON for multi-field connectors
            value = json.loads(decrypted)
            self._cache[type_str] = value
            return value
        except Exception:
            self._cache[type_str] = None
            return None
    
    def has(self, connector_type: ConnectorType | str) -> bool:
        """Check if a credential is configured (without decrypting)."""
        type_str = connector_type.value if isinstance(connector_type, ConnectorType) else connector_type
        
        return (
            self.db.query(ConnectorCredential)
            .filter(
                ConnectorCredential.agent_id == self.agent_id,
                ConnectorCredential.connector_type == type_str,
            )
            .count() > 0
        )
```

### API Endpoints

```python
# apps/zerg/backend/zerg/routers/agent_connectors.py

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime

from zerg.database import get_db
from zerg.dependencies.auth import get_current_user
from zerg.models.models import Agent, ConnectorCredential
from zerg.utils.crypto import encrypt, decrypt
from zerg.connectors.registry import CONNECTOR_REGISTRY, ConnectorType
from zerg.connectors.testers import test_connector

router = APIRouter(
    prefix="/agents/{agent_id}/connectors",
    tags=["agent-connectors"],
)

# --- Schemas ---

class ConnectorStatus(BaseModel):
    type: str
    name: str
    description: str
    category: str
    icon: str
    docs_url: str
    configured: bool
    display_name: Optional[str] = None
    test_status: str = "untested"
    last_tested_at: Optional[datetime] = None
    metadata: Optional[dict] = None

class ConnectorConfigureRequest(BaseModel):
    connector_type: str
    credentials: dict[str, str]  # Field key -> value
    display_name: Optional[str] = None

class ConnectorTestResponse(BaseModel):
    success: bool
    message: str
    metadata: Optional[dict] = None

# --- Endpoints ---

@router.get("/", response_model=list[ConnectorStatus])
def list_agent_connectors(
    agent_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """List all connector types and their configuration status for an agent."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent or agent.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    # Get configured credentials for this agent
    configured = {
        c.connector_type: c
        for c in db.query(ConnectorCredential).filter(ConnectorCredential.agent_id == agent_id).all()
    }
    
    result = []
    for conn_type, definition in CONNECTOR_REGISTRY.items():
        cred = configured.get(conn_type.value)
        result.append(ConnectorStatus(
            type=conn_type.value,
            name=definition["name"],
            description=definition["description"],
            category=definition["category"],
            icon=definition["icon"],
            docs_url=definition["docs_url"],
            configured=cred is not None,
            display_name=cred.display_name if cred else None,
            test_status=cred.test_status if cred else "untested",
            last_tested_at=cred.last_tested_at if cred else None,
            metadata=cred.metadata if cred else None,
        ))
    
    return result

@router.post("/", status_code=status.HTTP_201_CREATED)
def configure_connector(
    agent_id: int,
    request: ConnectorConfigureRequest,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Configure (create or update) a connector credential for an agent."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent or agent.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    # Validate connector type
    try:
        conn_type = ConnectorType(request.connector_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Unknown connector type: {request.connector_type}")
    
    # Validate required fields
    definition = CONNECTOR_REGISTRY[conn_type]
    for field in definition["fields"]:
        if field["required"] and field["key"] not in request.credentials:
            raise HTTPException(status_code=400, detail=f"Missing required field: {field['key']}")
    
    # Encrypt credentials as JSON
    import json
    encrypted = encrypt(json.dumps(request.credentials))
    
    # Upsert
    existing = (
        db.query(ConnectorCredential)
        .filter(
            ConnectorCredential.agent_id == agent_id,
            ConnectorCredential.connector_type == conn_type.value,
        )
        .first()
    )
    
    if existing:
        existing.encrypted_value = encrypted
        existing.display_name = request.display_name
        existing.test_status = "untested"
        existing.last_tested_at = None
        existing.metadata = None
    else:
        cred = ConnectorCredential(
            agent_id=agent_id,
            connector_type=conn_type.value,
            encrypted_value=encrypted,
            display_name=request.display_name,
        )
        db.add(cred)
    
    db.commit()
    return {"success": True}

@router.post("/{connector_type}/test", response_model=ConnectorTestResponse)
def test_agent_connector(
    agent_id: int,
    connector_type: str,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Test a configured connector credential."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent or agent.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    cred = (
        db.query(ConnectorCredential)
        .filter(
            ConnectorCredential.agent_id == agent_id,
            ConnectorCredential.connector_type == connector_type,
        )
        .first()
    )
    
    if not cred:
        raise HTTPException(status_code=404, detail="Connector not configured")
    
    # Decrypt and test
    import json
    try:
        decrypted = json.loads(decrypt(cred.encrypted_value))
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to decrypt credential")
    
    result = test_connector(ConnectorType(connector_type), decrypted)
    
    # Update test status
    cred.test_status = "success" if result["success"] else "failed"
    cred.last_tested_at = datetime.utcnow()
    if result.get("metadata"):
        cred.metadata = result["metadata"]
    db.commit()
    
    return ConnectorTestResponse(
        success=result["success"],
        message=result["message"],
        metadata=result.get("metadata"),
    )

@router.delete("/{connector_type}", status_code=status.HTTP_204_NO_CONTENT)
def delete_connector(
    agent_id: int,
    connector_type: str,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
):
    """Remove a connector credential from an agent."""
    agent = db.query(Agent).filter(Agent.id == agent_id).first()
    if not agent or agent.owner_id != current_user.id:
        raise HTTPException(status_code=404, detail="Agent not found")
    
    cred = (
        db.query(ConnectorCredential)
        .filter(
            ConnectorCredential.agent_id == agent_id,
            ConnectorCredential.connector_type == connector_type,
        )
        .first()
    )
    
    if not cred:
        raise HTTPException(status_code=404, detail="Connector not configured")
    
    db.delete(cred)
    db.commit()
```

### Connector Testers

```python
# apps/zerg/backend/zerg/connectors/testers.py

import httpx
from typing import Any

from zerg.connectors.registry import ConnectorType

def test_connector(connector_type: ConnectorType, credentials: dict[str, Any]) -> dict[str, Any]:
    """Test a connector credential by making a real API call.
    
    Returns:
        {"success": True/False, "message": str, "metadata": optional dict}
    """
    testers = {
        ConnectorType.SLACK: _test_slack,
        ConnectorType.DISCORD: _test_discord,
        ConnectorType.EMAIL: _test_email,
        ConnectorType.SMS: _test_sms,
        ConnectorType.GITHUB: _test_github,
        ConnectorType.JIRA: _test_jira,
        ConnectorType.LINEAR: _test_linear,
        ConnectorType.NOTION: _test_notion,
    }
    
    tester = testers.get(connector_type)
    if not tester:
        return {"success": False, "message": f"No tester for {connector_type}"}
    
    try:
        return tester(credentials)
    except Exception as e:
        return {"success": False, "message": f"Test failed: {str(e)}"}

def _test_slack(creds: dict) -> dict:
    """Send a test message to Slack webhook."""
    webhook_url = creds.get("webhook_url")
    if not webhook_url:
        return {"success": False, "message": "Missing webhook_url"}
    
    response = httpx.post(
        webhook_url,
        json={"text": "🔧 Zerg test message - your Slack webhook is working!"},
        timeout=10.0,
    )
    
    if response.status_code == 200:
        return {"success": True, "message": "Test message sent to Slack"}
    return {"success": False, "message": f"Slack returned {response.status_code}: {response.text}"}

def _test_discord(creds: dict) -> dict:
    """Send a test message to Discord webhook."""
    webhook_url = creds.get("webhook_url")
    if not webhook_url:
        return {"success": False, "message": "Missing webhook_url"}
    
    response = httpx.post(
        webhook_url,
        json={"content": "🔧 Zerg test message - your Discord webhook is working!"},
        timeout=10.0,
    )
    
    if response.status_code in (200, 204):
        return {"success": True, "message": "Test message sent to Discord"}
    return {"success": False, "message": f"Discord returned {response.status_code}: {response.text}"}

def _test_email(creds: dict) -> dict:
    """Validate Resend API key by listing domains."""
    api_key = creds.get("api_key")
    if not api_key:
        return {"success": False, "message": "Missing api_key"}
    
    response = httpx.get(
        "https://api.resend.com/domains",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=10.0,
    )
    
    if response.status_code == 200:
        domains = response.json().get("data", [])
        domain_names = [d.get("name") for d in domains]
        return {
            "success": True,
            "message": f"API key valid. Domains: {', '.join(domain_names) or 'none'}",
            "metadata": {"domains": domain_names},
        }
    return {"success": False, "message": f"Resend returned {response.status_code}"}

def _test_sms(creds: dict) -> dict:
    """Validate Twilio credentials by fetching account info."""
    account_sid = creds.get("account_sid")
    auth_token = creds.get("auth_token")
    if not account_sid or not auth_token:
        return {"success": False, "message": "Missing account_sid or auth_token"}
    
    response = httpx.get(
        f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}.json",
        auth=(account_sid, auth_token),
        timeout=10.0,
    )
    
    if response.status_code == 200:
        data = response.json()
        return {
            "success": True,
            "message": f"Connected to Twilio account: {data.get('friendly_name')}",
            "metadata": {"friendly_name": data.get("friendly_name")},
        }
    return {"success": False, "message": f"Twilio returned {response.status_code}"}

def _test_github(creds: dict) -> dict:
    """Validate GitHub token by fetching authenticated user."""
    token = creds.get("token")
    if not token:
        return {"success": False, "message": "Missing token"}
    
    response = httpx.get(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
        },
        timeout=10.0,
    )
    
    if response.status_code == 200:
        data = response.json()
        return {
            "success": True,
            "message": f"Connected as {data.get('login')}",
            "metadata": {"login": data.get("login"), "name": data.get("name")},
        }
    return {"success": False, "message": f"GitHub returned {response.status_code}"}

def _test_jira(creds: dict) -> dict:
    """Validate Jira credentials by fetching current user."""
    domain = creds.get("domain")
    email = creds.get("email")
    api_token = creds.get("api_token")
    if not all([domain, email, api_token]):
        return {"success": False, "message": "Missing domain, email, or api_token"}
    
    # Ensure domain format
    if not domain.endswith(".atlassian.net"):
        domain = f"{domain}.atlassian.net"
    
    response = httpx.get(
        f"https://{domain}/rest/api/3/myself",
        auth=(email, api_token),
        timeout=10.0,
    )
    
    if response.status_code == 200:
        data = response.json()
        return {
            "success": True,
            "message": f"Connected as {data.get('displayName')}",
            "metadata": {"displayName": data.get("displayName"), "emailAddress": data.get("emailAddress")},
        }
    return {"success": False, "message": f"Jira returned {response.status_code}"}

def _test_linear(creds: dict) -> dict:
    """Validate Linear API key by fetching viewer info."""
    api_key = creds.get("api_key")
    if not api_key:
        return {"success": False, "message": "Missing api_key"}
    
    response = httpx.post(
        "https://api.linear.app/graphql",
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
        },
        json={"query": "{ viewer { id name email } }"},
        timeout=10.0,
    )
    
    if response.status_code == 200:
        data = response.json()
        viewer = data.get("data", {}).get("viewer", {})
        if viewer:
            return {
                "success": True,
                "message": f"Connected as {viewer.get('name')}",
                "metadata": {"name": viewer.get("name"), "email": viewer.get("email")},
            }
    return {"success": False, "message": f"Linear returned {response.status_code}"}

def _test_notion(creds: dict) -> dict:
    """Validate Notion token by fetching user info."""
    api_key = creds.get("api_key")
    if not api_key:
        return {"success": False, "message": "Missing api_key"}
    
    response = httpx.get(
        "https://api.notion.com/v1/users/me",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Notion-Version": "2022-06-28",
        },
        timeout=10.0,
    )
    
    if response.status_code == 200:
        data = response.json()
        return {
            "success": True,
            "message": f"Connected as {data.get('name', 'Integration')}",
            "metadata": {"name": data.get("name"), "type": data.get("type")},
        }
    return {"success": False, "message": f"Notion returned {response.status_code}"}
```

### Tool Integration

**Before (current `slack_tools.py`):**

```python
def send_slack_webhook(
    webhook_url: str,  # Required - user must provide
    text: str,
    ...
) -> Dict[str, Any]:
    ...
```

**After (updated `slack_tools.py`):**

```python
from zerg.connectors.resolver import CredentialResolver
from zerg.connectors.registry import ConnectorType

def send_slack_webhook(
    text: str,
    blocks: Optional[List[Dict[str, Any]]] = None,
    attachments: Optional[List[Dict[str, Any]]] = None,
    unfurl_links: bool = True,
    unfurl_media: bool = True,
    *,
    _resolver: CredentialResolver,  # Injected by agent runner
) -> Dict[str, Any]:
    """Send a message to Slack via the configured webhook.
    
    Requires Slack to be configured in Agent Settings → Connectors.
    """
    creds = _resolver.get(ConnectorType.SLACK)
    if not creds:
        return {
            "success": False,
            "error": "Slack is not configured for this agent. Configure it in Agent Settings → Connectors.",
        }
    
    webhook_url = creds.get("webhook_url")
    # ... rest of implementation unchanged
```

**Agent Runner Integration:**

```python
# In zerg/agents_def/zerg_react_agent.py

from zerg.connectors.resolver import CredentialResolver

# When setting up tools for execution
resolver = CredentialResolver(agent_id=agent.id, db=db)

# Bind resolver to tools that need it
# (Implementation depends on LangGraph tool binding pattern)
```

---

## Implementation Plan

### Phase 1: Backend Foundation (Day 1-2)

| Task | Files |
|------|-------|
| Create Alembic migration | `alembic/versions/xxx_add_connector_credentials.py` |
| Add SQLAlchemy model | `zerg/models/models.py` |
| Create connector registry | `zerg/connectors/registry.py` |
| Create credential resolver | `zerg/connectors/resolver.py` |
| Create connector testers | `zerg/connectors/testers.py` |

### Phase 2: API Layer (Day 2-3)

| Task | Files |
|------|-------|
| Create API router | `zerg/routers/agent_connectors.py` |
| Add Pydantic schemas | `zerg/schemas/connector_schemas.py` |
| Register router in main | `zerg/main.py` |
| Add CRUD helpers | `zerg/crud/crud.py` |

### Phase 3: Tool Integration (Day 3-4)

| Task | Files |
|------|-------|
| Update Slack tool | `zerg/tools/builtin/slack_tools.py` |
| Update Discord tool | `zerg/tools/builtin/discord_tools.py` |
| Update Email tool | `zerg/tools/builtin/email_tools.py` |
| Update SMS tool | `zerg/tools/builtin/sms_tools.py` |
| Update GitHub tools | `zerg/tools/builtin/github_tools.py` |
| Update Jira tools | `zerg/tools/builtin/jira_tools.py` |
| Update Linear tools | `zerg/tools/builtin/linear_tools.py` |
| Update Notion tools | `zerg/tools/builtin/notion_tools.py` |
| Inject resolver in agent runner | `zerg/agents_def/zerg_react_agent.py` |

### Phase 4: Frontend UI (Day 4-6)

| Task | Files |
|------|-------|
| Create TypeScript types | `src/types/connectors.ts` |
| Create API hooks | `src/hooks/useAgentConnectors.ts` |
| Create ConnectorCard component | `src/components/agent-settings/ConnectorCard.tsx` |
| Create ConnectorConfigModal | `src/components/agent-settings/ConnectorConfigModal.tsx` |
| Create ConnectorCredentialsPanel | `src/components/agent-settings/ConnectorCredentialsPanel.tsx` |
| Add Connectors tab to drawer | `src/components/agent-settings/AgentSettingsDrawer.tsx` |

### Phase 5: Testing & Polish (Day 6-7)

| Task | Description |
|------|-------------|
| Backend unit tests | Test resolver, testers, API endpoints |
| Frontend component tests | Test modal, card, panel components |
| E2E test | Configure Slack → use tool → verify message sent |
| Manual QA | Test all 8 connectors with real credentials |

---

## Definition of Done

### Functional Requirements

- [ ] User can see all 8 connector types in Agent Settings → Connectors tab
- [ ] User can configure credentials for any connector type
- [ ] User can test credentials before saving (sends real test message/request)
- [ ] User can update existing credentials
- [ ] User can remove credentials
- [ ] Agent can execute connector tools without any credential parameters
- [ ] Agent shows helpful error when connector not configured
- [ ] Deleting an agent cascades to delete its credentials

### Security Requirements

- [ ] Credentials encrypted at rest using Fernet
- [ ] Credentials never returned to frontend (only masked display)
- [ ] Credentials never appear in chat logs or thread messages
- [ ] Only agent owner can configure/view credentials

### Technical Requirements

- [ ] Alembic migration creates table correctly
- [ ] Migration runs on both SQLite (dev) and PostgreSQL (prod)
- [ ] API endpoints follow existing patterns (auth, error handling)
- [ ] Frontend matches existing design system
- [ ] All existing tests pass
- [ ] New tests added for credential flow

---

## Appendix

### A. File Structure (Final State)

```
apps/zerg/backend/zerg/
├── connectors/
│   ├── __init__.py
│   ├── registry.py          # ConnectorType enum, CONNECTOR_REGISTRY
│   ├── resolver.py          # CredentialResolver class
│   └── testers.py           # test_connector() + 8 tester functions
├── models/
│   └── models.py            # + ConnectorCredential model
├── routers/
│   └── agent_connectors.py  # NEW: /agents/{id}/connectors endpoints
├── schemas/
│   └── connector_schemas.py # NEW: Pydantic models (optional, can inline)
└── tools/builtin/
    ├── slack_tools.py       # MODIFIED
    ├── discord_tools.py     # MODIFIED
    ├── email_tools.py       # MODIFIED
    ├── sms_tools.py         # MODIFIED
    ├── github_tools.py      # MODIFIED
    ├── jira_tools.py        # MODIFIED
    ├── linear_tools.py      # MODIFIED
    └── notion_tools.py      # MODIFIED

apps/zerg/frontend-web/src/
├── components/agent-settings/
│   ├── ConnectorCard.tsx              # NEW
│   ├── ConnectorConfigModal.tsx       # NEW
│   ├── ConnectorCredentialsPanel.tsx  # NEW
│   └── AgentSettingsDrawer.tsx        # MODIFIED (add tab)
├── hooks/
│   └── useAgentConnectors.ts          # NEW
└── types/
    └── connectors.ts                  # NEW

alembic/versions/
└── xxx_add_connector_credentials.py   # NEW
```

### B. Related Documentation

- `DEVELOPMENT.md` - Local development setup
- `DEPLOY.md` - Production deployment
- `docs/EMAIL_TOOL.md` - Resend email connector details
- `docs/JIRA_CONNECTOR.md` - Jira connector details
- `docs/LINEAR_CONNECTOR.md` - Linear connector details

### C. Changelog

| Date | Author | Changes |
|------|--------|---------|
| 2024-11-29 | David Rose & Claude | Initial research (`CONNECTOR_CREDENTIALS_UI.md`) |
| 2024-11-29 | David Rose & Claude | Revised to per-agent scope, this document |

