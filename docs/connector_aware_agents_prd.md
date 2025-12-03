# Connector-Aware Agents: Design & Implementation

> Making agents aware of which integrations are configured, so they can accurately represent their capabilities to users.

---

## Problem Statement

### Current Behavior

1. **Agent advertises ALL tools** regardless of configuration
   - Lists GitHub, Slack, Discord, Jira, Linear, Notion, Twilio, Resend, etc.
   - No awareness of which connectors the user has actually configured

2. **User asks "is GitHub connected?"**
   - Agent responds: "I don't know, let me try"
   - Forces trial-and-error discovery

3. **Tools fail at execution time**
   - User tries to use Slack → Tool fails → "Slack not configured"
   - Wasted API calls, poor UX, user frustration

4. **False advertising of capabilities**
   - Agent promises things it can't deliver
   - Erodes user trust

### Root Cause

**Static tool registration + dynamic credential availability = mismatch**

The agent has:
- Tool definitions (static, code-defined)
- No knowledge of connector configurations (dynamic, user-specific)
- No knowledge of credential validity (runtime state)

---

## Design Approaches Considered

| Approach | Description | Pros | Cons |
|----------|-------------|------|------|
| **Filtered** | Only register tools for configured connectors | Clean, no false promises | Agent can't discover what's *possible* |
| **Annotated** | Show all tools, mark status in context | Full discovery, proactive guidance | More context in prompt |
| **Fail-fast** | Try tools, fail with error (current) | Simple implementation | Worst UX, trial-and-error |
| **Hybrid** ✅ | Categorized (READY vs NEEDS_SETUP) + conditional enable | Best of both worlds | Medium complexity |

### Decision: Hybrid/Annotated Approach

- Tools for connected connectors are registered and callable
- Prompt context includes ALL connectors with their status
- Agent can distinguish "ready now" vs "available after setup"
- Agent can guide users to enable more capabilities

---

## Industry Alignment

This approach aligns with current best practices:

1. **Dynamic tool availability is first-class**
   - OpenAI's Agents SDK: `is_enabled` for runtime tool toggling
   - Function-calling guides recommend filtering by context/availability

2. **Structured over prose**
   - Machine-readable status blocks, not buried in text
   - Strongly typed tool metadata

3. **Transient vs persistent context**
   - Rules are persistent (system prompt, cached)
   - State is transient (per-turn injection)

4. **Discovery patterns**
   - MCP's `tools/list` for explicit capability enumeration
   - Meta-tools for runtime state refresh

5. **Structured prompts**
   - RISEN framework (Role, Instructions, Steps, Expectations, Narrowing)
   - XML sections for clear boundaries

---

## Architecture

### Cache-Aware Message Structure

```
┌─────────────────────────────────────────────┐
│ SYSTEM PROMPT (static, CACHED)              │
│ - Agent identity                            │
│ - <connector_protocol> interpretation rules │
│ - <error_handling> policies                 │
│ - <temporal_awareness> rules                │
├─────────────────────────────────────────────┤
│ CONTEXT INJECTION (dynamic, per-turn)       │  ← Cache breaks here
│ - <current_time>                            │
│ - <connector_status>                        │
├─────────────────────────────────────────────┤
│ CONVERSATION HISTORY (timestamped)          │
├─────────────────────────────────────────────┤
│ CURRENT USER MESSAGE                        │
└─────────────────────────────────────────────┘
```

**Key insight:** Rules in system prompt (cached), data injected per-turn (always fresh)

### Design Principle

> **Engineering problems are easier to solve than AI problems.**
>
> If you can give the model better context with a DB query, API call, or compute — do it. Don't make the AI compensate for missing information. Optimize infrastructure later.

**Applied:** Always query fresh connector status per-turn. A 5ms DB query beats AI hallucination from stale state.

---

## Format Specification

### Why XML Tags + JSON Data

- XML tags give clear section boundaries (LLMs parse these well)
- JSON inside is machine-readable and precise
- Easy to extract programmatically if needed
- Claude specifically handles XML-tagged sections very well

### Connector Status Block (Per-Turn Injection)

```xml
<connector_status captured_at="2025-01-17T15:00:00Z">
{
  "github": {
    "status": "connected",
    "scopes": ["repo", "issues"],
    "tools": ["github_create_issue", "github_list_repos", "github_get_pr"]
  },
  "notion": {
    "status": "connected",
    "scopes": ["pages:read"],
    "tools": ["notion_query_database", "notion_get_page"]
  },
  "slack": {
    "status": "not_configured",
    "setup_url": "/settings/integrations/slack",
    "would_enable": ["Send messages to channels", "Post thread replies"]
  },
  "jira": {
    "status": "invalid_credentials",
    "error": "OAuth token expired",
    "setup_url": "/settings/integrations/jira",
    "would_enable": ["Create tickets", "Update issues", "Add comments"]
  },
  "discord": {
    "status": "not_configured",
    "setup_url": "/settings/integrations/discord",
    "would_enable": ["Send messages", "Post to channels"]
  },
  "linear": {
    "status": "disabled_by_admin",
    "reason": "Organization policy"
  }
}
</connector_status>
```

### Status Values

| Status | Meaning | Agent Behavior |
|--------|---------|----------------|
| `connected` | Credentials valid, tools callable | Can use tools, offer capabilities |
| `not_configured` | User hasn't set up | Mention as available, offer setup_url |
| `invalid_credentials` | Token expired/revoked | Explain issue, suggest reconnecting |
| `rate_limited` | Temporarily unavailable | Explain, suggest waiting |
| `disabled_by_admin` | Organization policy | Do not offer or mention |

### Current Time Injection

Always include current time for temporal awareness:

```xml
<current_time>2025-01-17T15:00:00Z</current_time>
```

---

## System Prompt Components (Static, Cached)

### Connector Protocol

```xml
<connector_protocol>
You receive connector status in a <connector_status> block each turn.

Status interpretation:
- "connected" → tools are CALLABLE, you can use these capabilities
- "not_configured" → mention as available, offer setup_url for user to configure
- "invalid_credentials" → explain the issue, suggest user reconnect in settings
- "rate_limited" → explain temporary unavailability, suggest waiting
- "disabled_by_admin" → do NOT offer or mention these capabilities

Rules:
- NEVER promise actions for non-connected connectors
- NEVER call tools that require a non-connected connector
- Always be accurate about what you can do RIGHT NOW vs what's POSSIBLE after setup
</connector_protocol>
```

### Capability Presentation Protocol

```xml
<capability_protocol>
When the user asks "what can you do?" or similar questions, follow this format:

**Ready now:**
- [Connector]: [specific capabilities using connected tools]

**Available after setup:**
- [Connector]: [what it would enable] → [setup guidance]

Example response:

I can help you with:

**Ready now:**
- GitHub: create issues, list repositories, review pull requests, comment on PRs
- Notion: query databases, retrieve pages, search workspace

**Available after setup:**
- Slack: send messages, post to channels → Connect in Settings → Integrations
- Jira: create tickets, update issues → Reconnect (credentials expired)

Be concise. Don't list every single tool, group by capability area.
</capability_protocol>
```

### Error Handling Protocol

```xml
<error_handling>
Tool errors return a structured envelope:
{"ok": false, "error_type": "...", "user_message": "..."}

When you receive an error:
1. Do NOT retry the same call without user action
2. Explain the failure using the provided user_message
3. If error_type indicates a connector problem:
   - "connector_not_configured" → guide to setup_url
   - "invalid_credentials" → suggest reconnecting
   - "rate_limited" → explain wait time if known
   - "permission_denied" → explain what scope is missing
4. Tell the user what fixing the issue would enable

Never silently fail. Always surface errors clearly.
</error_handling>
```

### Temporal Awareness Protocol

```xml
<temporal_awareness>
You receive <current_time> and <connector_status captured_at="..."> each turn.

Be aware that:
- Conversations can span minutes, hours, or days between messages
- Connector status is always fresh (captured this turn)
- If a user mentions "I just connected X" or "I set up Y", trust the fresh status

Use timestamps to understand conversation context:
- Large time gaps may mean the user's context has changed
- Recent messages are most relevant for immediate intent
</temporal_awareness>
```

---

## Implementation Details

### Per-Turn Context Building (Pseudo-code)

```python
async def build_agent_context(user_id: str) -> str:
    # Always query fresh - engineering > AI problems
    connector_status = await get_connector_status(user_id)
    current_time = datetime.utcnow().isoformat() + "Z"

    return f"""
<current_time>{current_time}</current_time>

<connector_status captured_at="{current_time}">
{json.dumps(connector_status, indent=2)}
</connector_status>
"""

async def get_connector_status(user_id: str) -> dict:
    """Query all connectors and their status for this user."""
    connectors = await db.fetch_all(
        "SELECT connector_type, status, scopes, error_message "
        "FROM user_connectors WHERE user_id = :user_id",
        {"user_id": user_id}
    )

    # Build status dict with all possible connectors
    status = {}
    for connector_type in ALL_CONNECTOR_TYPES:
        user_connector = next(
            (c for c in connectors if c.connector_type == connector_type),
            None
        )

        if user_connector and user_connector.status == "connected":
            status[connector_type] = {
                "status": "connected",
                "scopes": user_connector.scopes,
                "tools": get_tools_for_connector(connector_type)
            }
        elif user_connector and user_connector.status == "invalid_credentials":
            status[connector_type] = {
                "status": "invalid_credentials",
                "error": user_connector.error_message,
                "setup_url": f"/settings/integrations/{connector_type}",
                "would_enable": get_capabilities_for_connector(connector_type)
            }
        else:
            status[connector_type] = {
                "status": "not_configured",
                "setup_url": f"/settings/integrations/{connector_type}",
                "would_enable": get_capabilities_for_connector(connector_type)
            }

    return status
```

### Message Assembly

```python
async def build_messages(
    user_id: str,
    system_prompt: str,  # Static, includes all protocols
    conversation_history: list[Message],
    current_message: str
) -> list[dict]:

    # Build fresh context
    context = await build_agent_context(user_id)

    # Assemble messages
    messages = [
        {"role": "system", "content": system_prompt},  # Cached
        {"role": "user", "content": context},          # Fresh context injection
        {"role": "assistant", "content": "Understood. I'm aware of the current connector status."},
        *[msg.to_dict() for msg in conversation_history],
        {"role": "user", "content": current_message}
    ]

    return messages
```

### Standardized Error Envelope

All tools should return errors in this format:

```python
class ToolError:
    ok: bool = False
    error_type: str  # connector_not_configured, invalid_credentials, rate_limited, etc.
    user_message: str  # Human-readable explanation
    connector: str | None  # Which connector failed
    setup_url: str | None  # Where to fix it

# Example tool implementation
async def slack_send_message(channel: str, text: str) -> dict:
    connector = await get_connector("slack")

    if not connector:
        return {
            "ok": False,
            "error_type": "connector_not_configured",
            "user_message": "Slack is not connected. Set it up in Settings → Integrations → Slack.",
            "connector": "slack",
            "setup_url": "/settings/integrations/slack"
        }

    if connector.status == "invalid_credentials":
        return {
            "ok": False,
            "error_type": "invalid_credentials",
            "user_message": "Slack credentials have expired. Please reconnect in Settings.",
            "connector": "slack",
            "setup_url": "/settings/integrations/slack"
        }

    # Actual implementation...
    result = await slack_client.post_message(channel, text)
    return {"ok": True, "data": result}
```

---

## Optional: Meta-Tool for Explicit Refresh

While per-turn injection eliminates most staleness issues, a meta-tool is useful for explicit verification:

```xml
<meta_tools>
You have access to `refresh_connector_status` which returns the latest connector status.

Call this when:
- User explicitly asks to verify their connections
- You want to confirm a connector is working before a critical action
- User says "check if X is connected"

This is rarely needed since connector_status is refreshed every turn, but useful for explicit verification.
</meta_tools>
```

```python
@tool
async def refresh_connector_status(user_id: str) -> dict:
    """Fetch and return current connector status."""
    return await get_connector_status(user_id)
```

---

## Temporal Awareness: The Staleness Problem

### Problem

LLMs have no inherent sense of time. A conversation that looks like:

```
Turn 1 (Monday 9am):    User: "Send this to Slack"
Turn 2 (Monday 9am):    Agent: "Slack isn't configured..."
Turn 3 (Friday 3pm):    User: "Ok send it now"
Turn 4 (Friday 3pm):    Agent: "As I mentioned, Slack isn't configured..." ← WRONG
```

The agent has no idea 4 days passed and the user may have configured Slack.

### Solution: Per-Turn Fresh Injection

By always injecting fresh `connector_status` every turn, this problem is eliminated. The agent always has accurate, current state.

### Optional Enhancement: Message Timestamps

For additional context, timestamp conversation messages:

```xml
<conversation>
  <message role="user" ts="2025-01-13T09:00:00Z">Send this to Slack</message>
  <message role="assistant" ts="2025-01-13T09:00:05Z">Slack isn't configured...</message>
  <message role="user" ts="2025-01-17T15:00:00Z">Ok send it now</message>
</conversation>
```

Now agent can reason about time gaps, though with fresh connector status this is less critical.

---

## Open Design Decisions

| Decision | Options | Recommendation |
|----------|---------|----------------|
| **Tool registration** | Filter unconfigured vs register all + mark disabled | Filter out, but list in prompt context as "available" |
| **Refresh frequency** | Per-session vs per-turn | Per-turn (always fresh) |
| **Error envelope** | Standardize `{ok, error_type, user_message}` | Yes, standardize all tool errors |
| **Scope visibility** | Include scopes in status | Yes (enables "can read but not write") |
| **Message timestamps** | Add to all messages | Yes (low effort, high value) |
| **Multi-connector tools** | What if tool needs GitHub AND Slack? | Check all required, fail with specific missing connector |

---

## Implementation Priority

| Change | Effort | Impact | Priority |
|--------|--------|--------|----------|
| Per-turn `connector_status` injection | Low | High | **P0** |
| `current_time` injection | Trivial | High | **P0** |
| `captured_at` on status | Trivial | Medium | **P0** |
| Static protocols in system prompt | Medium | High | **P0** |
| Standardized error envelope | Medium | Medium | **P1** |
| Timestamps on messages | Medium | Medium | **P1** |
| Capability presentation protocol | Low | Medium | **P1** |
| Meta-tool for refresh | Medium | Low | **P2** |
| Tool filtering based on status | Medium | Medium | **P2** |

---

## Testing Considerations

### Scenarios to Verify

1. **No connectors configured**
   - Agent correctly lists everything as "available after setup"
   - No tools are callable, agent doesn't attempt them

2. **Partial configuration**
   - Agent distinguishes connected vs not configured
   - Only calls tools for connected connectors

3. **Invalid credentials**
   - Agent recognizes expired tokens
   - Guides user to reconnect

4. **Mid-session connector change**
   - User connects new service
   - Next turn shows fresh status (per-turn injection)

5. **Tool failure handling**
   - Tool returns error envelope
   - Agent surfaces user_message appropriately

6. **"What can you do?" query**
   - Agent uses capability presentation protocol
   - Correctly categorizes ready vs available

7. **Long conversation gaps**
   - Days between messages
   - Agent uses fresh connector status, doesn't reference stale state

---

## Future Considerations

### Not Yet Covered

1. **Multi-connector tools** — What if a tool needs GitHub AND Slack?
2. **Partial failures** — GitHub connected but rate-limited mid-session
3. **Scope granularity** — "Can read issues but can't write" scenarios
4. **Admin vs user disabled** — Different messaging for each
5. **Workspace-level connectors** — Shared connectors across team members
6. **Connector health checks** — Proactive validation of credentials

### Potential Enhancements

1. **Connector onboarding flow** — Agent guides through setup interactively
2. **Capability suggestions** — "Based on your workflow, connecting Slack would let you..."
3. **Usage analytics** — Track which connectors are most used, suggest relevant ones

---

## Summary

This design makes agents **connector-aware** by:

1. **Injecting fresh connector status every turn** (engineering > AI)
2. **Using structured XML+JSON format** for clear boundaries
3. **Separating rules (cached) from data (per-turn)** for efficiency
4. **Providing clear protocols** for capability presentation and error handling
5. **Including temporal context** (`current_time`, `captured_at`) for awareness

The result: Agents accurately represent what they can do NOW, guide users to enable MORE, and never promise capabilities they can't deliver.

---

## Implementation Status

> Last updated: 2025-12-02

### P0 Items - ✅ COMPLETE

| Item | Status | Implementation |
|------|--------|----------------|
| Per-turn `connector_status` injection | ✅ Done | `zerg/connectors/status_builder.py` - `build_connector_status()` |
| `current_time` injection | ✅ Done | `build_agent_context()` includes ISO 8601 timestamp |
| `captured_at` on status | ✅ Done | XML attribute on `<connector_status>` block |
| Static protocols in system prompt | ✅ Done | `zerg/prompts/connector_protocols.py` |
| Agent runner integration | ✅ Done | `zerg/managers/agent_runner.py` - `run_thread()` |
| Unit tests | ✅ Done | 53 tests in `tests/connectors/` and `tests/prompts/` |

### P1 Items - ✅ COMPLETE

| Item | Status | Implementation |
|------|--------|----------------|
| Standardized error envelope | ✅ Done | `zerg/tools/error_envelope.py` + all 9 connector tools updated |
| Timestamps on messages | ✅ Done | `zerg/services/thread_service.py` - ISO 8601 prefix on messages |
| Capability presentation protocol | ✅ Done | Included in P0 protocols |

### Key Files

```
apps/zerg/backend/zerg/
├── connectors/
│   └── status_builder.py      # build_connector_status(), build_agent_context()
├── prompts/
│   └── connector_protocols.py # Static protocol definitions (XML blocks)
├── tools/
│   ├── error_envelope.py      # Standardized error/success response types
│   └── builtin/               # All 9 connector tools updated with envelope
├── services/
│   └── thread_service.py      # Message timestamps added
└── managers/
    └── agent_runner.py        # Integration point (protocol prepend + context injection)

apps/zerg/backend/tests/
├── connectors/
│   └── test_status_builder.py # 23 tests for status builder
├── prompts/
│   └── test_connector_protocols.py # 30 tests for protocols
└── tools/
    └── test_error_envelope.py # 19 tests for error envelope
```

### Git Commits

**P0 Commits:**
1. `51d0618` - feat(connectors): add connector status builder for agent context
2. `425a110` - feat(agents): inject connector status context into agent turns
3. `018db82` - feat(prompts): add connector-aware protocol definitions
4. `579ae26` - test(connectors): add tests for connector-aware agent context

**P1 Commits:**
5. `e400120` - feat(tools): add standardized error envelope module
6. `de9ccb7` - feat(tools): standardize github tools to use error envelope
7. `e277ee2` - feat(tools): standardize slack tools to use error envelope
8. `a76bd77` - test(tools): add comprehensive tests for error envelope module
9. `5e71a0e` - feat(tools): standardize discord/email/sms tools to use error envelope
10. `31e1cc3` - feat(tools): standardize jira tools to use error envelope
11. `f2b145d` - feat(tools): standardize linear/notion/imessage tools to use error envelope
12. `f5599fd` - feat(agents): add timestamps to conversation history messages

**Bug Fix Commits:**
13. `4f1edbd` - fix(connectors): check credential test_status in status_builder
14. `37c45fb` - fix(tools): complete jira_tools error envelope migration
15. `128e8b1` - fix(tests): update tool tests for new error envelope contract
16. `8c30c59` - fix(tests): update ThreadService tests for timestamp prefix
17. `15e7ede` - fix(tests): update status_builder tests for direct DB queries
18. `a4e87ea` - fix(connectors): remove unused imports and variables (lint)

**P2 Commits:**
19. `d5ef8fb` - feat(tools): add refresh_connector_status meta-tool
20. `18b9cde` - feat(connectors): add get_unavailable_tools helper for tool filtering

### P2 Items - ✅ COMPLETE

| Item | Status | Implementation |
|------|--------|----------------|
| Meta-tool for refresh | ✅ Done | `refresh_connector_status` tool in `connector_tools.py` |
| Tool filtering helper | ✅ Done | `get_unavailable_tools()` in `status_builder.py` |

**Note on tool filtering:** Full runtime filtering would require changes to the cached agent runnable architecture. The current approach (protocols + error envelope) provides equivalent UX: agents know not to call disconnected tools, and if they do, they get clear errors.

### Architecture Notes

The implementation follows the PRD's cache-aware message structure:

```
[System message with protocols] ← Static, prepended at runtime
    ↓
[Context injection: current_time + connector_status] ← Dynamic, per-turn
    ↓
[Conversation history]
    ↓
[Current user message]
```

**Key design decisions:**
1. Protocols prepended to system message (not stored in DB)
2. Context injected as ephemeral user/assistant pair (not persisted)
3. Graceful degradation if context building fails
4. Synchronous DB queries (5ms acceptable per PRD)
