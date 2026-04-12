import { Link } from "react-router-dom";
import { usePageMeta } from "../../hooks/usePageMeta";
import { CodeBlock } from "./CodeBlock";

export default function IntegrationsPage() {
  usePageMeta({
    title: "Integrations - Longhouse Docs",
    description: "Supported CLI agents: Claude Code, Codex CLI, Gemini CLI, and more.",
  });

  return (
    <>
      <h1>Integrations</h1>
      <p className="docs-subtitle">
        Longhouse reads the session files your CLI tools already produce.
        No plugins or provider-side configuration needed. Starting through
        Longhouse keeps the control path explicit instead of pretending every
        provider is equally mature.
      </p>

      <h2>Claude Code</h2>
      <p>
        <strong>Strongest today.</strong> Claude has the best end-to-end story:
        import, search, raw session detail, and the strongest control-after-launch
        path when started through Longhouse.
      </p>
      <CodeBlock title="terminal">
        {`longhouse claude               # start with control channel`}
      </CodeBlock>
      <p>
        Claude Code sessions are imported from{" "}
        <code>~/.claude/projects/</code>. Longhouse reads the JSONL session
        files Claude produces and indexes every message, tool call, and output.
      </p>
      <table>
        <thead>
          <tr>
            <th>Capability</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          <tr><td>Session import</td><td>Full</td></tr>
          <tr><td>Search & detail</td><td>Full</td></tr>
          <tr><td>Live control (wall, tail, message)</td><td>Strongest today</td></tr>
          <tr><td>Continue / branch</td><td>Strongest today</td></tr>
        </tbody>
      </table>

      <h2>Codex CLI</h2>
      <p>
        Archive and search are solid, and launch-through-Longhouse is supported.
        Codex can stay reachable after launch, but the continuation path is still
        catching up to Claude.
      </p>
      <CodeBlock title="terminal">
        {`longhouse codex                # start with control channel`}
      </CodeBlock>
      <table>
        <thead>
          <tr>
            <th>Capability</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          <tr><td>Session import</td><td>Full</td></tr>
          <tr><td>Search & detail</td><td>Full</td></tr>
          <tr><td>Launch through Longhouse</td><td>Supported</td></tr>
          <tr><td>Live control</td><td>Supported</td></tr>
          <tr><td>Continue / branch</td><td>Supported, maturing</td></tr>
        </tbody>
      </table>

      <h2>Gemini CLI</h2>
      <p>
        Treat Gemini as archive and search first today. Longhouse ingests the
        sessions cleanly, but live control and continuation are not the reason
        to buy on Gemini yet.
      </p>
      <table>
        <thead>
          <tr>
            <th>Capability</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          <tr><td>Session import</td><td>Full</td></tr>
          <tr><td>Search & detail</td><td>Full</td></tr>
          <tr><td>Launch through Longhouse</td><td>Early</td></tr>
          <tr><td>Live control</td><td>Not yet</td></tr>
          <tr><td>Continue / branch</td><td>Not yet</td></tr>
        </tbody>
      </table>

      <h2>Coming Soon</h2>
      <ul>
        <li><strong>OpenCode</strong> — timeline import and hosted workflows</li>
        <li><strong>Cursor</strong> — IDE-integrated AI sessions</li>
      </ul>

      <h2>How import works</h2>
      <p>
        Longhouse watches for new session files and imports them into the local
        SQLite database. Sessions are deduplicated by provider session ID, so
        re-importing is safe and idempotent.
      </p>
      <p>
        The background shipping service (<code>longhouse connect --install</code>)
        handles automatic imports and repairs the local hook path. You can also
        trigger a one-time import with:
      </p>
      <CodeBlock title="terminal">{`longhouse ship`}</CodeBlock>

      <h2>MCP Server</h2>
      <p>
        Longhouse includes a built-in MCP server that exposes session search,
        recall, and coordination to any MCP-compatible host:
      </p>
      <CodeBlock title="terminal">{`longhouse mcp-server`}</CodeBlock>
      <p>
        This is the same <Link to="/docs/api">Machine API</Link> surface
        exposed over the MCP protocol. Add it to your Claude Code or Codex
        MCP configuration to give your agent access to session history.
      </p>
    </>
  );
}
