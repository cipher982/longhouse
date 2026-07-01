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
        Cursor sessions land in the timeline two ways. Bare{" "}
        <code>cursor-agent</code> runs import from{" "}
        <code>~/.cursor/chats</code> as unmanaged, searchable history. Launching
        through Longhouse with <code>longhouse cursor</code> starts a{" "}
        <strong>Helm</strong> session: the same interactive{" "}
        <code>cursor-agent</code> TUI in your terminal, with a background control
        channel and a live transcript streamed to the timeline as turns commit.
        From the web or iOS you can send, interrupt, and terminate. Headless
        one-shot launches (Console mode) are also available via the web/iOS
        launch modal using Cursor&rsquo;s ACP surface.
      </p>
      <CodeBlock title="terminal">
        {`longhouse cursor              # start Cursor Agent (steerable TUI + live transcript)
longhouse cursor import        # backfill bare cursor-agent sessions from ~/.cursor/chats`}
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
          <tr><td>Launch through Longhouse (Helm)</td><td>Supported</td></tr>
          <tr><td>Headless launch (Console / ACP)</td><td>Supported</td></tr>
          <tr><td>Live control</td><td>Send, interrupt, and terminate</td></tr>
          <tr><td>Live transcript</td><td>Supported (Helm)</td></tr>
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
