import { usePageMeta } from "@/shared/hooks/usePageMeta";
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

      <h3>Ship transcripts</h3>
      <CodeBlock title="POST /api/agents/storage/v2/envelopes">
        {`# The Machine Agent ships transcripts as storage-v2 envelopes
# (X-Longhouse-Storage-Lane: live|repair). That is the engine's wire format,
# not a scripting contract. To import a file by hand:
longhouse-server ship --file path/to/session.jsonl

# GET /api/agents/storage/v2/capabilities returns the protocol version,
# ingest path, and size limits for the calling machine
curl -H "X-Agents-Token: $LONGHOUSE_DEVICE_TOKEN" \\
  http://localhost:8080/api/agents/storage/v2/capabilities`}
      </CodeBlock>

      <h3>List sessions</h3>
      <CodeBlock title="GET /api/agents/sessions">
        {`curl "http://localhost:8080/api/agents/sessions?query=auth+retry&limit=10"

# Query parameters:
#   query              - search query
#   limit              - max results (default 20, max 100)
#   offset             - pagination offset
#   project            - filter by project name
#   provider           - filter by provider id (claude, codex, cursor, opencode, pi, omp, antigravity)
#   environment        - filter by environment (production, development, test, e2e)
#   device_id          - filter by device ID
#   days_back          - look back N days; with a query, omit to search all
#                         indexed history (default: 14-day window when listing
#                         without a query)
#   include_test       - include test/e2e sessions (default: false)
#   hide_autonomous    - hide sub-agents (default: true)
#   mode               - search mode: lexical|semantic|hybrid (default: lexical)
#   sort               - sort order: relevance|recency|balanced`}
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

      <h3>Send directed input</h3>
      <CodeBlock title="POST /api/agents/directed-inputs">
        {`curl -X POST http://localhost:8080/api/agents/directed-inputs \\
  -H "X-Agents-Token: $LONGHOUSE_COORDINATION_TOKEN" \\
  -H "X-Longhouse-Session-Id: $SOURCE_SESSION_ID" \\
  -H "Content-Type: application/json" \\
  -d '{"target_session_id": "TARGET_SESSION_ID", "text": "Check the failing test", "client_request_id": "check-test-1"}'

# Persists before any safe-boundary delivery attempt`}
      </CodeBlock>

      <h3>Recover directed input</h3>
      <CodeBlock title="GET /api/agents/directed-inputs">
        {`curl "http://localhost:8080/api/agents/directed-inputs?direction=inbound&after_id=0&limit=20" \\
  -H "X-Agents-Token: $LONGHOUSE_COORDINATION_TOKEN" \\
  -H "X-Longhouse-Session-Id: $CURRENT_SESSION_ID"

# Query parameters:
#   direction  - inbound|outbound|all (default: inbound)
#   after_id   - stable input cursor (default: 0)
#   limit      - max results (default 50, max 200)`}
      </CodeBlock>

      <h3>Reply to directed input</h3>
      <CodeBlock title="POST /api/agents/directed-inputs/:id/reply">
        {`curl -X POST http://localhost:8080/api/agents/directed-inputs/INPUT_ID/reply \\
  -H "X-Agents-Token: $LONGHOUSE_COORDINATION_TOKEN" \\
  -H "X-Longhouse-Session-Id: $CURRENT_SESSION_ID" \\
  -H "Content-Type: application/json" \\
  -d '{"text": "The test is fixed", "client_request_id": "check-test-reply-1"}'`}
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


      <h3>Send presence signal</h3>
      <CodeBlock title="POST /api/agents/presence">
        {`curl -X POST http://localhost:8080/api/agents/presence \\
  -H "X-Agents-Token: YOUR_DEVICE_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"session_id": "SESSION_ID", "state": "running", "provider": "claude"}'

# state: thinking|running|idle|needs_user|blocked|stalled. Returns 204.`}
      </CodeBlock>

      <h2>Health</h2>

      <h3>Health check</h3>
      <CodeBlock title="GET /api/health">
        {`curl http://localhost:8080/api/health

# Returns status (healthy, degraded, or unhealthy with HTTP 503) and the
# build identity. Loopback, admin, and internal callers also get per-check detail.`}
      </CodeBlock>

      <h3>Readiness</h3>
      <CodeBlock title="GET /api/readyz">
        {`curl http://localhost:8080/api/readyz

# Readiness: returns 200 when the catalog service answers, 503 otherwise`}
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
