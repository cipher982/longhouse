import { Link } from "react-router-dom";
import { usePageMeta } from "../../hooks/usePageMeta";
import { CodeBlock } from "./CodeBlock";

export default function IntegrationsPage() {
  usePageMeta({
    title: "Integrations - Longhouse Docs",
    description: "Supported CLI agents: Claude Code, Codex CLI, Cursor Agent, Antigravity CLI, and OpenCode.",
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
        Antigravity sessions are observed as Shadow sessions. Native Helm is
        explicitly excluded until one native runtime owns the hook and control contract.
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
          <tr><td>Launch through Longhouse</td><td>Excluded from native Helm</td></tr>
          <tr><td>Phase hooks</td><td>Excluded from native Helm</td></tr>
          <tr><td>Live control</td><td>Unavailable</td></tr>
          <tr><td>Continue / branch</td><td>Not yet</td></tr>
        </tbody>
      </table>

      <h2>OpenCode</h2>
      <p>
        OpenCode lands in the timeline alongside the other CLIs. Launch through
        Longhouse with <code>longhouse opencode</code> for a managed-control
        session: archive, transcript, process-level health, remote send,
        interrupt, and lifecycle terminate. Active-turn steer and pause-answer
        are not advertised yet.
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
          <tr><td>Live control</td><td>Send, interrupt, and lifecycle control</td></tr>
          <tr><td>Continue / branch</td><td>Not yet</td></tr>
        </tbody>
      </table>

      <h2>Cursor Agent</h2>
      <p>
        Cursor sessions are observed as Shadow sessions. Native Helm is
        explicitly excluded until one native runtime owns PTY, control,
        permission, and transcript lifecycle.
      </p>
      <table>
        <thead>
          <tr>
            <th>Capability</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody>
          <tr><td>Session import / archive</td><td>Native storage-v2 migration in progress</td></tr>
          <tr><td>Search & detail</td><td>Available after native archive ships</td></tr>
          <tr><td>Launch through Longhouse (Helm)</td><td>Excluded from native Helm</td></tr>
          <tr><td>Headless launch (Console / ACP)</td><td>Unavailable during archive migration</td></tr>
          <tr><td>Live control</td><td>Unavailable</td></tr>
          <tr><td>Live transcript</td><td>Unavailable until receipt-backed source proof</td></tr>
          <tr><td>Continue / branch</td><td>Not yet</td></tr>
        </tbody>
      </table>

      <h2>How import works</h2>
      <p>
        Longhouse watches for new session files and imports them into the local
        SQLite database. Sessions are deduplicated by provider session ID, so
        re-importing is safe and idempotent.
      </p>
      <p>
        The native Machine Agent service is installed with <code>longhouse machine repair --repair-service</code> after <code>longhouse auth</code>.
        Runtime Host compatibility tooling can trigger a one-time import with:
      </p>
      <CodeBlock title="terminal">{`longhouse-python ship`}</CodeBlock>

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
