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

      <h3>List sessions</h3>
      <CodeBlock title="GET /api/agents/sessions">
        {`curl "http://localhost:8080/api/agents/sessions?query=auth+retry&limit=10"

# Query parameters:
#   query    - search query
#   limit    - max results (default 50)
#   offset   - pagination offset
#   project  - filter by project name
#   provider - filter by provider (claude, codex, gemini)`}
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
        {`curl "http://localhost:8080/api/agents/sessions/SESSION_ID/events?limit=100"`}
      </CodeBlock>
      <p>
        Returns the raw event stream — messages, tool calls, tool outputs, and
        system events in chronological order.
      </p>

      <h2>Coordination</h2>

      <h3>Wall (active sessions)</h3>
      <CodeBlock title="GET /api/agents/sessions/wall">
        {`curl http://localhost:8080/api/agents/sessions/wall`}
      </CodeBlock>
      <p>Returns active and recently active sessions.</p>

      <h3>Send a message</h3>
      <CodeBlock title="POST /api/agents/messages">
        {`curl -X POST http://localhost:8080/api/agents/messages \\
  -H "Content-Type: application/json" \\
  -d '{"target_session_id": "SESSION_ID", "content": "Check the failing test"}'`}
      </CodeBlock>

      <h3>Continue a session</h3>
      <CodeBlock title="POST /api/agents/sessions/:id/branch-cloud">
        {`curl -X POST http://localhost:8080/api/agents/sessions/SESSION_ID/branch-cloud \\
  -H "X-Agents-Token: YOUR_DEVICE_TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"prompt": "Continue where this left off"}'`}
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
