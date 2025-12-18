# Inline Connection UX Improvements

**Status**: Proposed
**Created**: 2024-12-17
**Context**: Competitor research (Tasklet + Pipedream)

## Background

Competitor analysis revealed a polished UX for connection/credential management directly within the chat interface, rather than requiring users to navigate to settings.

### Competitor Features Observed

1. **Inline credential capture** - Agent detects when credentials are shared in chat, shows a modal
2. **Human-in-the-loop approval** - "Create new connection?" modal with Connect/Deny buttons
3. **Tool access disclosure** - Shows "Requesting access to additional tools: Execute a Command"
4. **Account-level storage** - "Saved to your account for easy use in other agents"
5. **Pipedream backend** - Uses Pipedream iPaaS for OAuth, credential vault, 2000+ integrations

### Current Zerg Architecture

| Component         | What It Does                                          | Limitation                                      |
| ----------------- | ----------------------------------------------------- | ----------------------------------------------- |
| **Connectors**    | Per-agent encrypted credentials (Slack, GitHub, etc.) | Configured in Settings drawer only, not in chat |
| **ssh_exec**      | Execute commands on remote servers                    | Legacy; requires SSH keys on backend            |
| **Runner system** | User-owned execution infrastructure                   | Secure but setup is text-only, not interactive  |

## Proposed Improvements

### 1. Inline Runner Setup Card (Priority: High)

**Problem**: Runner setup instructions are plain text in chat, not interactive.

**Solution**: Render runner setup as an interactive card component.

```
┌─────────────────────────────────────────────────────────────┐
│ 🔌 Connect Your Infrastructure                              │
│                                                             │
│ I need to run commands on your servers. Run this on any    │
│ machine with access to your infrastructure:                 │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ docker run -d --name swarmlet-runner \                  │ │
│ │   -e SWARMLET_URL=https://api.swarmlet.io \             │ │
│ │   -e RUNNER_ID=abc123 \                                 │ │
│ │   -e RUNNER_SECRET=xyz789 \                             │ │
│ │   swarmlet/runner:latest                                │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ [📋 Copy Command]                                           │
│                                                             │
│ Status: ⏳ Waiting for connection...                        │
│         (polling for runner to come online)                 │
└─────────────────────────────────────────────────────────────┘
```

**Implementation**:

1. Backend: `runner_create_enroll_token()` already returns setup commands
2. Frontend: Detect structured output in message, render `<RunnerSetupCard>`
3. Frontend: Poll `/api/runners` until runner appears online
4. Frontend: Update card to show "✅ Connected!" when ready

**Effort**: 2-3 hours

---

### 2. SSH Connector Type (Priority: Medium)

**Problem**: With hardcoded hosts removed, there's no way for users to configure SSH targets.

**Solution**: Add SSH as a connector type in the registry.

```python
ConnectorType.SSH = "ssh"

CONNECTOR_REGISTRY[ConnectorType.SSH] = {
    "name": "SSH Server",
    "description": "Execute commands on a remote server via SSH",
    "category": "infrastructure",
    "fields": [
        {"key": "host", "label": "Hostname", "type": "text", "required": True},
        {"key": "port", "label": "Port", "type": "text", "placeholder": "22", "required": False},
        {"key": "username", "label": "Username", "type": "text", "required": True},
        {"key": "private_key", "label": "Private Key", "type": "password", "required": True},
    ],
}
```

**Security Considerations**:

- Private keys stored encrypted in DB
- Backend needs network access to user targets
- Consider recommending Runner system instead for multi-tenant

**Effort**: 1-2 hours

---

### 3. Account-Level Credentials UI (Priority: Medium)

**Problem**: Backend supports account-level credentials but no UI to manage them.

**Current**: `CredentialResolver` already checks account-level fallback:

```python
# Resolution order:
# 1. Agent-level override (connector_credentials table)
# 2. Account-level credential (account_connector_credentials table)
```

**Solution**:

1. Add "Connections" section in user profile/settings
2. Show all configured connectors at account level
3. Add "Save to account" checkbox in agent connector modal

**Effort**: 2 hours

---

### 4. Tool Access Disclosure (Priority: Low)

**Problem**: Users don't know what tools a connector enables.

**Solution**: Show tool list in connector config modal:

```
┌─────────────────────────────────────────────────────────────┐
│ Configure SSH Connection                                    │
│                                                             │
│ Hostname: [________________________]                        │
│ Username: [________________________]                        │
│ Private Key: [________________________]                     │
│                                                             │
│ ┌─────────────────────────────────────────────────────────┐ │
│ │ 🔧 This will enable:                                    │ │
│ │    • ssh_exec - Execute commands on this server         │ │
│ └─────────────────────────────────────────────────────────┘ │
│                                                             │
│ [Test] [Save]                                               │
└─────────────────────────────────────────────────────────────┘
```

**Effort**: 30 minutes

---

### 5. Inline Connection Approval Modal (Priority: Future)

**Problem**: Agent can't request new connections during conversation.

**Solution**: Add action request system for interactive chat components.

**Backend**:

```python
def request_connection(connector_type: str, suggested_config: dict) -> dict:
    """Agent tool to request user approval for a new connection."""
    return {
        "action_type": "create_connection",
        "connector": connector_type,
        "suggested": suggested_config,
        "awaiting_approval": True
    }
```

**Frontend**:

- Detect `action_required` in message metadata
- Render appropriate inline card (connection approval, permission request, etc.)
- User action triggers API call
- Result flows back to agent via thread

**Effort**: 4-6 hours (requires new component system)

---

## Architecture Decision: Pipedream Integration?

**Option A: Use Pipedream**

- Pros: 2000+ integrations, battle-tested OAuth, managed credential vault
- Cons: Third-party dependency, costs scale with usage, data flows through them
- Cost: ~$19-99/mo per workspace

**Option B: Build In-House (Current)**

- Pros: Full control, no external dependency, no per-call costs
- Cons: More work per integration, maintain OAuth flows ourselves

**Option C: Hybrid**

- Use Pipedream for complex OAuth (Google, Microsoft, Salesforce)
- Keep simple ones in-house (webhooks, API keys)
- Keep Runner system for infrastructure execution

**Recommendation**: Start with in-house improvements (Options 1-4 above). Evaluate Pipedream later if OAuth complexity becomes a bottleneck.

---

## Implementation Order

1. **Inline Runner Setup Card** - Best bang for buck, improves existing flow
2. **SSH Connector Type** - Needed now that hardcoded hosts are removed
3. **Account-Level Credentials UI** - Convenience improvement
4. **Tool Access Disclosure** - Quick win for transparency
5. **Inline Approval Modal** - Future, when we want agent-initiated connections

---

## Related Files

### Backend

- `apps/zerg/backend/zerg/connectors/registry.py` - Connector type definitions
- `apps/zerg/backend/zerg/routers/agent_connectors.py` - Credential management API
- `apps/zerg/backend/zerg/routers/runners.py` - Runner enrollment/management
- `apps/zerg/backend/zerg/tools/builtin/ssh_tools.py` - SSH execution (now dynamic-only)
- `apps/zerg/backend/zerg/tools/builtin/runner_tools.py` - Runner execution

### Frontend

- `apps/zerg/frontend-web/src/components/chat/ChatMessageList.tsx` - Message rendering
- `apps/zerg/frontend-web/src/components/agent-settings/ConnectorCredentialsPanel.tsx` - Connector config UI
- `apps/zerg/frontend-web/src/components/agent-settings/ConnectorConfigModal.tsx` - Credential modal
