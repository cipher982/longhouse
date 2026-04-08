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
        No plugins or provider-side configuration needed.
      </p>

      <h2>Claude Code</h2>
      <p>
        <strong>Best support today.</strong> Full session import, search, detail,
        and remote control when started through Longhouse.
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
          <tr><td>Remote control (wall, tail, message)</td><td>Full</td></tr>
          <tr><td>Continue / branch</td><td>Full</td></tr>
        </tbody>
      </table>

      <h2>Codex CLI</h2>
      <p>
        Session import, search, detail, and remote control.
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
          <tr><td>Remote control</td><td>Full</td></tr>
          <tr><td>Continue / branch</td><td>Full</td></tr>
        </tbody>
      </table>

      <h2>Gemini CLI</h2>
      <p>
        Session import and search/detail. Remote control support is in
        progress.
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
          <tr><td>Remote control</td><td>Coming soon</td></tr>
          <tr><td>Continue / branch</td><td>Coming soon</td></tr>
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
        handles automatic imports. You can also trigger a one-time import with:
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
