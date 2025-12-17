# Inline Connection UX Improvements

**Status:** Draft
**Author:** David Rose
**Created:** December 2024
**Based on:** Competitor analysis (Tasklet, Pipedream)

---

## Summary

This spec outlines proposed UX improvements for connection/credential management in Swarmlet, inspired by competitor analysis of Tasklet and Pipedream. The goal is to make runner setup and credential configuration more discoverable, interactive, and chat-first.

---

## Proposed Features (Priority Order)

### 1. Inline Runner Setup Card (P0)

**Problem:** When users need to execute commands on their infrastructure, the current flow requires them to navigate to a separate dashboard UI or manually interpret text-based setup instructions in chat.

**Solution:** Render an interactive "Runner Setup Card" directly in the chat interface when the agent/supervisor detects no runners are available or when the user asks to connect infrastructure.

**UX Flow:**
1. User asks: "Check disk space on prod" or "Connect my laptop"
2. Supervisor detects no runners available for this user
3. Instead of plain text, Supervisor emits a structured message containing runner setup data
4. Frontend renders an interactive card with:
   - Copy button for the setup commands
   - Real-time connection status polling
   - Visual confirmation when runner comes online
   - Direct link to dashboard for advanced configuration

**Card Components:**
```
┌─────────────────────────────────────────────────────┐
│ 🖥️  Connect Your Infrastructure                     │
├─────────────────────────────────────────────────────┤
│                                                     │
│ Run this on your machine to connect:                │
│                                                     │
│ ┌─────────────────────────────────────────────────┐ │
│ │ curl -X POST https://api.swarmlet.com/api/...   │ │
│ │   -H 'Content-Type: application/json' \         │ │
│ │   -d '{"enroll_token": "abc123..."}'            │ │
│ └─────────────────────────────────────────────────┘ │
│                                      [📋 Copy]      │
│                                                     │
│ Status: ⏳ Waiting for connection...                │
│                                                     │
│ Token expires in: 9:42                              │
│                                                     │
└─────────────────────────────────────────────────────┘
```

**When runner connects:**
```
┌─────────────────────────────────────────────────────┐
│ ✅ Runner Connected!                                │
├─────────────────────────────────────────────────────┤
│                                                     │
│ "my-laptop" is now online and ready                 │
│                                                     │
│ Capabilities: exec.readonly                         │
│ Last seen: just now                                 │
│                                                     │
│ [Configure Runner] [Continue Task]                  │
│                                                     │
└─────────────────────────────────────────────────────┘
```

**Implementation:**
- Backend: New structured message type `runner_setup_card` emitted via WebSocket
- Frontend: New React component `RunnerSetupCard.tsx` in chat components
- Polling: Use existing `/api/runners` endpoint to check for new online runners
- Alternatively: Subscribe to runner status via WebSocket

**Data Schema:**
```typescript
interface RunnerSetupCardData {
  type: "runner_setup_card";
  enroll_token: string;
  expires_at: string;  // ISO timestamp
  swarmlet_url: string;
  docker_command: string;
  poll_interval_ms?: number;  // Default 3000
}
```

---

### 2. SSH Connector Type (P1)

**Problem:** After removing hardcoded SSH hosts from the backend, users need a way to configure SSH targets dynamically. Currently, Runners can SSH from their location, but users can't easily define named SSH targets.

**Solution:** Add a new "SSH" connector type that allows users to configure SSH targets (host, user, key reference) which Runners can use to reach remote machines.

**Use Case:**
- User has Runner on laptop
- Laptop has SSH access to `prod-web-1`, `prod-db-1`, etc. via `~/.ssh/config`
- User configures SSH targets in Swarmlet dashboard
- Agents can reference targets by name: "run df -h on prod-web-1"

**Data Model:**
```python
class SSHConnectorCredential:
    connector_type = "ssh"
    fields = [
        {"key": "host", "label": "Host", "type": "text", "required": True},
        {"key": "user", "label": "Username", "type": "text", "required": False},
        {"key": "port", "label": "Port", "type": "text", "required": False, "default": "22"},
        {"key": "ssh_config_name", "label": "SSH Config Name", "type": "text", "required": False},
        # Note: We don't store keys - runner uses its local SSH config/agent
    ]
```

**Tool Integration:**
```python
def runner_ssh_exec(target: str, command: str, timeout_secs: int = 30):
    """Execute command on SSH target via Runner.

    Args:
        target: Name of configured SSH target or runner:id
        command: Shell command to execute
        timeout_secs: Timeout in seconds
    """
    # Resolve target to SSH host/user from connector credentials
    # Dispatch to runner with SSH metadata
```

**Implementation Notes:**
- SSH targets are stored in AccountConnectorCredential (account-level, not agent-level)
- Runner daemon handles actual SSH execution using its local SSH config
- No private keys stored in Swarmlet backend

---

### 3. Account-Level Credentials UI (P1)

**Problem:** Users must configure credentials for each agent separately. Most users want "configure once, use everywhere."

**Current State:** Account-level credentials infrastructure EXISTS in backend (`AccountConnectorCredential` model, `/api/account/connectors/*` endpoints). The `IntegrationsPage.tsx` provides a basic UI. However:
- Not prominently linked in navigation
- Doesn't show which agents inherit which credentials
- Missing clear hierarchy explanation (account → agent override)

**Solution:** Enhance the existing Integrations page:

1. **Add to main navigation** (Settings → Integrations)
2. **Show credential inheritance:**
   ```
   ┌─────────────────────────────────────────────────────┐
   │ 🔑 GitHub (Account-wide)                           │
   ├─────────────────────────────────────────────────────┤
   │ Connected as: @drose-d                              │
   │ Used by: Agent-1, Agent-2, Agent-3                  │
   │                                                     │
   │ Agent-4 has its own override ↗                      │
   │                                                     │
   │ [Test] [Edit] [Delete]                              │
   └─────────────────────────────────────────────────────┘
   ```

3. **Clear hierarchy docs:** Tooltip/docs explaining:
   - Account credentials apply to all your agents by default
   - Per-agent credentials override account-level
   - Use case: Different GitHub tokens for personal vs work agents

---

### 4. Tool Access Disclosure (P2)

**Problem:** Users configure connectors but don't know which tools each connector enables. This creates confusion: "I added GitHub, why can't my agent see PRs?"

**Solution:** Show tool capabilities on connector cards.

**UI Enhancement:**
```
┌─────────────────────────────────────────────────────┐
│ 🐙 GitHub                                    ✓ API  │
├─────────────────────────────────────────────────────┤
│ Connected as: @drose-d                              │
│                                                     │
│ Enabled Tools:                                      │
│ • github_create_issue     • github_list_issues      │
│ • github_add_comment      • github_get_issue        │
│ • github_list_pull_requests • github_get_pull_request│
│                                                     │
│ [Test] [Edit] [Delete]                              │
└─────────────────────────────────────────────────────┘
```

**Implementation:**
- Extend `CONNECTOR_REGISTRY` with `enabled_tools` field
- Frontend maps connector types to tool lists
- Show collapsed by default, expandable

---

### 5. Inline Approval Modal (P3 - Future)

**Problem:** Agents may need credentials that haven't been configured yet. Currently they fail with "connector not configured" errors.

**Solution:** Enable agents to REQUEST connections with human-in-the-loop approval.

**UX Flow:**
1. Agent needs GitHub access but it's not configured
2. Agent emits structured approval request message
3. User sees modal:
   ```
   ┌─────────────────────────────────────────────────────┐
   │ 🔐 Connection Request                               │
   ├─────────────────────────────────────────────────────┤
   │                                                     │
   │ Your agent wants to use GitHub.                     │
   │                                                     │
   │ This will allow:                                    │
   │ • Creating and managing issues                      │
   │ • Reading pull requests                             │
   │ • Adding comments                                   │
   │                                                     │
   │ Would you like to connect GitHub now?               │
   │                                                     │
   │ [Connect GitHub] [Not Now] [Never Ask]              │
   │                                                     │
   └─────────────────────────────────────────────────────┘
   ```
4. User clicks "Connect GitHub" → OAuth or credential modal
5. Agent resumes with new credentials

**Implementation (Future):**
- New message type: `connection_request`
- Interrupt/resume pattern for agent execution
- Track user preferences ("Never Ask" for specific connectors)

---

## Technical Architecture

### Structured Chat Messages

To support rich UI cards in chat, introduce a structured message envelope:

```typescript
// New message types
type StructuredMessageType =
  | "runner_setup_card"
  | "connection_request"
  | "tool_result_card"
  | "approval_request";

interface StructuredMessage {
  id: string;
  role: "assistant" | "system";
  type: StructuredMessageType;
  data: Record<string, unknown>;
  // Optional text fallback for non-supporting clients
  text_fallback?: string;
}
```

### Backend Changes

1. **Runner setup tool enhancement:**
   - `runner_create_enroll_token` returns structured data
   - WebSocket emits `runner_setup_card` message type
   - Include polling endpoint or subscribe to runner status

2. **SSH connector type:**
   - Add to `CONNECTOR_REGISTRY`
   - Create test function for SSH connectivity
   - Integration with `runner_exec` for SSH target resolution

3. **Tool-connector mapping:**
   - Extend registry with `enabled_tools` per connector
   - API endpoint to get tools for a connector type

### Frontend Changes

1. **Chat message renderer:**
   - Handle structured message types
   - Render appropriate card components

2. **New components:**
   - `RunnerSetupCard.tsx` - Interactive runner onboarding
   - `ConnectionRequestModal.tsx` - HITL approval (future)
   - Enhanced `ConnectorCard.tsx` - Tool disclosure

3. **Polling/subscription:**
   - Poll runner status during setup
   - Or: Subscribe to runner WebSocket channel

---

## Migration Path

### Phase 1: Inline Runner Setup Card
1. Create `RunnerSetupCard` component
2. Modify `runner_create_enroll_token` tool to emit structured message
3. Frontend renders card from structured data
4. Add polling for runner status

### Phase 2: SSH Connector + Account Credentials Polish
1. Add SSH connector type to registry
2. Implement SSH target resolution in runner tools
3. Enhance IntegrationsPage with inheritance display
4. Add to main navigation

### Phase 3: Tool Disclosure + Future HITL
1. Add `enabled_tools` to connector registry
2. Update ConnectorCard to show tools
3. (Future) Implement approval request flow

---

## Open Questions

1. **Polling vs WebSocket for runner status?**
   - Polling: Simpler, works everywhere
   - WebSocket: Real-time, but adds complexity
   - Recommendation: Start with polling, migrate to WS if needed

2. **SSH key storage?**
   - Option A: Store encrypted keys in AccountConnectorCredential
   - Option B: Never store keys, rely on runner's local SSH agent
   - Recommendation: Option B for security, document requirement

3. **Tool disclosure granularity?**
   - Show all tools enabled by connector?
   - Or only tools agent is allowed to use?
   - Recommendation: Show all enabled, indicate which are allowed

---

## Success Metrics

- **Runner setup time:** Reduce from ~5 minutes (find docs, copy commands) to <1 minute (copy from card)
- **Connection success rate:** Track completion from setup card shown → runner online
- **Credential configuration:** % of users with account-level credentials vs agent-only
- **Tool usage after disclosure:** Users enabling more tools after seeing capabilities

---

## Related Documents

- `execution-connectors-v1.md` - Runner architecture spec
- `docs/archive/CONNECTOR_CREDENTIALS_UI.md` - Original credentials UI design
- `super-siri-architecture.md` - Overall system architecture
