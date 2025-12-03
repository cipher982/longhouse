"""Static protocol definitions for connector-aware agents.

These protocols are injected into the agent system prompt (static, cacheable)
to define how the agent should interpret dynamic connector status injected per-turn.

Per the connector-aware agents PRD, these protocols are:
- STATIC: Part of the system prompt, eligible for prompt caching
- RULES: Define how to interpret dynamic data
- SEPARATE from the dynamic <connector_status> which is injected per-turn
"""

# ---------------------------------------------------------------------------
# Protocol Definitions
# ---------------------------------------------------------------------------

CONNECTOR_PROTOCOL = """<connector_protocol>
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
</connector_protocol>"""

CAPABILITY_PROTOCOL = """<capability_protocol>
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
</capability_protocol>"""

ERROR_HANDLING_PROTOCOL = """<error_handling>
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
</error_handling>"""

TEMPORAL_AWARENESS_PROTOCOL = """<temporal_awareness>
You receive <current_time> and <connector_status captured_at="..."> each turn.
Conversation messages are timestamped in ISO 8601 format (e.g., [2025-01-17T15:00:00Z]).

Be aware that:
- Conversations can span minutes, hours, or days between messages
- Connector status is always fresh (captured this turn)
- If a user mentions "I just connected X" or "I set up Y", trust the fresh status

Use timestamps to understand conversation context:
- Large time gaps may mean the user's context has changed
- Recent messages are most relevant for immediate intent
- Timestamp format: [YYYY-MM-DDTHH:MM:SSZ] at start of user/assistant messages
</temporal_awareness>"""

# ---------------------------------------------------------------------------
# Combined Protocol Assembly
# ---------------------------------------------------------------------------


def get_connector_protocols() -> str:
    """Return all connector protocols as a single string.

    These protocols define how the agent should interpret the dynamic
    connector status that is injected per-turn. The protocols themselves
    are static and should be included in the system prompt for caching.

    Returns:
        str: All protocols concatenated with newlines for readability
    """
    return "\n\n".join(
        [
            CONNECTOR_PROTOCOL,
            CAPABILITY_PROTOCOL,
            ERROR_HANDLING_PROTOCOL,
            TEMPORAL_AWARENESS_PROTOCOL,
        ]
    )
