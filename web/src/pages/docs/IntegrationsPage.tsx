import { Link } from "react-router-dom";
import { usePageMeta } from "../../hooks/usePageMeta";
import { CodeBlock } from "./CodeBlock";

export default function IntegrationsPage() {
  usePageMeta({
    title: "Integrations - Longhouse Docs",
    description: "Supported CLI agents: Claude Code, Codex CLI, Antigravity CLI, and OpenCode.",
  });

  return (
    <>
      <h1>Integrations</h1>
      <p className="docs-subtitle">
        Longhouse reads the session files your CLI tools already produce.
        No plugins or provider-side configuration needed. Bare CLI runs import
        as unmanaged history; launching through Longhouse creates managed
        sessions and keeps the control path explicit. Import exists so
        Longhouse is useful immediately, but starting through Longhouse is the
        path we want users on.
      </p>

      <h2>Claude Code</h2>
      <p>
        <strong>Strongest today.</strong> Claude has the best end-to-end story:
        import, search, raw session detail, and the strongest control-after-launch
        path when launched through Longhouse.
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
        Archive and search are solid, and managed launch through Longhouse is
        supported. Bare Codex runs still import as unmanaged history. Codex can
        stay reachable after launch, but the continuation path is still
        catching up to Claude. For new work, prefer <code>longhouse codex</code>{" "}
        over bare <code>codex</code>.
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

      <h2>Antigravity CLI</h2>
      <p>
        Antigravity is Google's new CLI path. Longhouse launches it with{" "}
        <code>longhouse agy</code>, installs a small Antigravity plugin
        for hook-backed phase signals, and binds its transcript to the managed
        Longhouse session when hooks expose the transcript path.
      </p>
      <CodeBlock title="terminal">
        {`longhouse agy                  # start Antigravity CLI with Longhouse session ownership`}
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
          <tr><td>Phase hooks</td><td>Supported</td></tr>
          <tr><td>Live control</td><td>Observe-only today</td></tr>
          <tr><td>Continue / branch</td><td>Not yet</td></tr>
        </tbody>
      </table>

      <h2>OpenCode</h2>
      <p>
        OpenCode lands in the timeline alongside the other CLIs. Launch through
        Longhouse with <code>longhouse opencode</code> for a managed
        observe-only session: archive, transcript, and process-level health
        without remote send controls today.
      </p>
      <CodeBlock title="terminal">
        {`longhouse opencode             # start OpenCode with Longhouse session ownership`}
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
          <tr><td>Live control</td><td>Observe-only today</td></tr>
          <tr><td>Continue / branch</td><td>Not yet</td></tr>
        </tbody>
      </table>

      <h2>Legacy Gemini CLI</h2>
      <p>
        Gemini CLI remains a legacy archive path. Longhouse keeps the parser
        and import behavior so existing sessions stay searchable, but new Google
        CLI work should move to Antigravity.
      </p>

      <h2>How import works</h2>
      <p>
        Longhouse watches for new session files and imports them into the local
        SQLite database. Sessions are deduplicated by provider session ID, so
        re-importing is safe and idempotent.
      </p>
      <p>
        The background shipping service is installed with <code>longhouse connect --install</code>.
        Once a machine is already linked, use <code>longhouse machine repair</code> to
        repair the local hook path and shipping runtime. You can also trigger a
        one-time import with:
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
