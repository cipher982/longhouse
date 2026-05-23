import { usePageMeta } from "../../hooks/usePageMeta";
import { CodeBlock } from "./CodeBlock";

export default function MachineAPIPage() {
  usePageMeta({
    title: "Machine API - Longhouse Docs",
    description: "The /api/agents/* HTTP surface for scripts, tools, and integrations.",
  });

  return (
    <>
      <h1>Machine API</h1>
      <p className="docs-subtitle">
        The <code>/api/agents/*</code> surface is the canonical machine
        contract. The browser, CLI, and MCP server all sit on top of it.
      </p>

      <h2>Authentication</h2>
      <p>
        Local dev defaults to no auth. For production, machine clients
        authenticate with a device token:
      </p>
      <CodeBlock title="terminal">
        {`curl -H "X-Agents-Token: YOUR_DEVICE_TOKEN" \\
  http://localhost:8080/api/agents/sessions`}
      </CodeBlock>
      <p>
        Device tokens are managed from the Devices page in the browser or via
        the API. Browser access uses cookie-based auth separately.
      </p>

      <h2>Sessions</h2>

      <h3>Ingest sessions</h3>
      <CodeBlock title="POST /api/agents/ingest">
        {`curl -X POST http://localhost:8080/api/agents/ingest \\
  -H "X-Agents-Token: YOUR_DEVICE_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d @session.json

# Accepts gzip-compressed payloads: Content-Encoding: gzip
# Creates or updates a session and inserts events with deduplication`}
      </CodeBlock>

      <h3>List sessions</h3>
      <CodeBlock title="GET /api/agents/sessions">
        {`curl "http://localhost:8080/api/agents/sessions?query=auth+retry&limit=10"

# Query parameters:
#   query              - search query
#   limit              - max results (default 50)
#   offset             - pagination offset
#   project            - filter by project name
#   provider           - filter by provider (claude, codex, antigravity, opencode; gemini for legacy archives)
#   environment        - filter by environment (production, development, test, e2e)
#   device_id          - filter by device ID
#   days_back          - look back N days (default 14, max 90)
#   include_test       - include test/e2e sessions (default: false)
#   hide_autonomous    - hide sub-agents (default: true)
#   mode               - search mode: lexical|semantic|hybrid (default: lexical)
#   sort               - sort order: relevance|recency|balanced`}
      </CodeBlock>

      <h3>List session summaries</h3>
      <CodeBlock title="GET /api/agents/sessions/summary">
        {`curl "http://localhost:8080/api/agents/sessions/summary?project=zerg&limit=20"

# Returns compact session metadata for picker UIs`}
      </CodeBlock>

      <h3>List active sessions</h3>
      <CodeBlock title="GET /api/agents/sessions/active">
        {`curl "http://localhost:8080/api/agents/sessions/active?project=zerg"

# Returns recently active sessions for live monitoring`}
      </CodeBlock>

      <h3>Get session detail</h3>
      <CodeBlock title="GET /api/agents/sessions/:id">
        {`curl http://localhost:8080/api/agents/sessions/SESSION_ID`}
      </CodeBlock>
      <p>
        Returns full session metadata, event count, timing, and project
        context.
      </p>

      <h3>Get session events</h3>
      <CodeBlock title="GET /api/agents/sessions/:id/events">
        {`curl "http://localhost:8080/api/agents/sessions/SESSION_ID/events?limit=100"

# Query parameters:
#   limit          - max results (default 100, max 1000)
#   offset         - pagination offset
#   roles          - comma-separated roles to filter (assistant, user, system, tool)
#   tool_name      - exact tool name filter (e.g., Bash)
#   query          - content search within session
#   context_mode   - forensic|active_context
#   branch_mode    - head|all (include abandoned branches)`}
      </CodeBlock>
      <p>
        Returns the raw event stream — messages, tool calls, tool outputs, and
        system events in chronological order.
      </p>

      <h3>Get session tail</h3>
      <CodeBlock title="GET /api/agents/sessions/:id/tail">
        {`curl "http://localhost:8080/api/agents/sessions/SESSION_ID/tail?limit=30"

# Returns the last N events from a session
# Useful for cross-session reading of recent activity`}
      </CodeBlock>

      <h3>Get session preview</h3>
      <CodeBlock title="GET /api/agents/sessions/:id/preview">
        {`curl "http://localhost:8080/api/agents/sessions/SESSION_ID/preview?last_n=6"

# Returns compact preview of recent messages (for UI cards)`}
      </CodeBlock>

      <h3>Get session thread</h3>
      <CodeBlock title="GET /api/agents/sessions/:id/thread">
        {`curl "http://localhost:8080/api/agents/sessions/SESSION_ID/thread"

# Returns all continuations in the logical thread`}
      </CodeBlock>

      <h3>Get session projection</h3>
      <CodeBlock title="GET /api/agents/sessions/:id/projection">
        {`curl "http://localhost:8080/api/agents/sessions/SESSION_ID/projection?branch_mode=head"

# Returns the stitched lineage-path projection for a focused session
# Combines thread with events in one view`}
      </CodeBlock>

      <h3>Get session workspace</h3>
      <CodeBlock title="GET /api/agents/sessions/:id/workspace">
        {`curl "http://localhost:8080/api/agents/sessions/SESSION_ID/workspace"

# Returns focused session, thread, and projection in one round trip
# Optimized for single HTTP call on session open`}
      </CodeBlock>

      <h3>Export session</h3>
      <CodeBlock title="GET /api/agents/sessions/:id/export">
        {`curl "http://localhost:8080/api/agents/sessions/SESSION_ID/export?branch_mode=head" \\
  > session.jsonl

# Export session as JSONL for Claude Code --resume`}
      </CodeBlock>

      <h2>Coordination</h2>

      <h3>Wall (active sessions)</h3>
      <CodeBlock title="GET /api/agents/sessions/wall">
        {`curl "http://localhost:8080/api/agents/sessions/wall?project=zerg&days=7"

# Query parameters:
#   repo       - filter by git_repo (substring match)
#   project    - filter by project name
#   days       - look back N days (default 7)
#   limit      - max results (default 50, max 200)`}
      </CodeBlock>
      <p>Returns raw signal metadata for active and recently active sessions.</p>

      <h3>Get filter options</h3>
      <CodeBlock title="GET /api/agents/filters">
        {`curl "http://localhost:8080/api/agents/filters?days_back=90"

# Returns distinct projects, providers, and machine IDs for UI dropdowns`}
      </CodeBlock>

      <h3>Send a message</h3>
      <CodeBlock title="POST /api/agents/messages">
        {`curl -X POST http://localhost:8080/api/agents/messages \\
  -H "Content-Type: application/json" \\
  -d '{"to_session_id": "SESSION_ID", "text": "Check the failing test"}'

# Requires X-Longhouse-Session-Id header or from_session_id in body`}
      </CodeBlock>

      <h3>List messages</h3>
      <CodeBlock title="GET /api/agents/messages">
        {`curl "http://localhost:8080/api/agents/messages?session_id=SESSION_ID"

# Query parameters:
#   session_id           - session to inspect
#   direction            - inbound|outbound|all (default: inbound)
#   unacknowledged_only  - only undelivered messages (default: false)
#   limit                - max results (default 50, max 200)`}
      </CodeBlock>

      <h3>Acknowledge message</h3>
      <CodeBlock title="POST /api/agents/messages/:id/ack">
        {`curl -X POST http://localhost:8080/api/agents/messages/MESSAGE_ID/ack`}
      </CodeBlock>

      <h3>Send live message to active session</h3>
      <CodeBlock title="POST /api/agents/sessions/:id/send-live">
        {`curl -X POST http://localhost:8080/api/agents/sessions/SESSION_ID/send-live \\
  -H "X-Agents-Token: YOUR_DEVICE_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"text": "Check that test"}'

# Sends message to an actively running session (if connected)`}
      </CodeBlock>

      <h3>Set session action</h3>
      <CodeBlock title="POST /api/agents/sessions/:id/action">
        {`curl -X POST http://localhost:8080/api/agents/sessions/SESSION_ID/action \\
  -H "Content-Type: application/json" \\
  -d '{"action": "park"}'

# Actions: park|snooze|archive|resume`}
      </CodeBlock>

      <h3>Set loop mode</h3>
      <CodeBlock title="PATCH /api/agents/sessions/:id/loop-mode">
        {`curl -X PATCH http://localhost:8080/api/agents/sessions/SESSION_ID/loop-mode \\
  -H "Content-Type: application/json" \\
  -d '{"loop_mode": "continuous"}'`}
      </CodeBlock>

      <h3>Send presence signal</h3>
      <CodeBlock title="POST /api/agents/presence">
        {`curl -X POST http://localhost:8080/api/agents/presence \\
  -H "X-Agents-Token: YOUR_DEVICE_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"device_id": "DEVICE_ID", "work_state": "working"}'`}
      </CodeBlock>

      <h2>Health</h2>

      <h3>Health check</h3>
      <CodeBlock title="GET /api/health">
        {`curl http://localhost:8080/api/health

# Returns: server status, uptime, database stats, write serializer metrics`}
      </CodeBlock>

      <h3>Readiness</h3>
      <CodeBlock title="GET /api/readyz">
        {`curl http://localhost:8080/api/readyz

# Lightweight check: returns 200 if the database is reachable`}
      </CodeBlock>

      <h2>Response format</h2>
      <p>
        All endpoints return JSON. List endpoints support <code>limit</code>{" "}
        and <code>offset</code> for pagination. Errors return a JSON object
        with a <code>detail</code> field.
      </p>
      <div className="docs-callout">
        <p>
          <strong>Same surface everywhere.</strong> The browser, CLI, and MCP
          server are all thin wrappers around these endpoints. Anything you can
          do in the browser, you can script against the API.
        </p>
      </div>
    </>
  );
}
