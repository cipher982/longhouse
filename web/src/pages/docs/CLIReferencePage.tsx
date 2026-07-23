import { usePageMeta } from "../../hooks/usePageMeta";
import { CodeBlock } from "./CodeBlock";

export default function CLIReferencePage() {
  usePageMeta({
    title: "CLI Reference - Longhouse Docs",
    description: "Every Longhouse CLI command, flag, and output format.",
  });

  return (
    <>
      <h1>CLI Reference</h1>
      <p className="docs-subtitle">
        The native <code>longhouse</code> CLI owns device setup and managed
        provider sessions. <code>longhouse-server</code> runs the Runtime Host.
      </p>

      <h2>Core Commands</h2>

      <h3>longhouse-server serve</h3>
      <p>Start the Runtime Host from its server environment.</p>
      <CodeBlock title="terminal">
        {`longhouse-server serve                # default: localhost:8080
longhouse-server serve --port 9090    # custom port
longhouse-server serve --demo         # start with demo data`}
      </CodeBlock>

      <h3>longhouse claude / codex / opencode</h3>
      <p>
        Launch a managed provider CLI session with a Longhouse control channel.
        The session runs in your terminal and stays reachable from other
        surfaces.
      </p>
      <CodeBlock title="terminal">
        {`longhouse claude               # start Claude Code (steerable)
longhouse codex                # start Codex CLI (steerable)
longhouse opencode             # start OpenCode (managed live control)`}
      </CodeBlock>
      <p>
        Use these as the default launch path for new work. Bare{" "}
        <code>claude</code>, <code>codex</code>, <code>antigravity</code>,{" "}
        <code>opencode</code>, and <code>cursor-agent</code> runs still import
        into the timeline, but they remain unmanaged history. Claude and Codex
        support the strongest live control. OpenCode Helm supports managed send, interrupt, and terminate but
        not active-turn steer. Cursor and
        Antigravity are Shadow-only in the native device release.
      </p>

      <h3>longhouse-server ship</h3>
      <p>
        One-time import of existing session files into the timeline. Useful
        when you want to backfill history from a machine.
      </p>
      <CodeBlock title="terminal">
        {`longhouse-server ship                 # import all detected sessions`}
      </CodeBlock>

      <h2>Runtime Host commands</h2>

      <h3>longhouse-server recall</h3>
      <p>Semantic search — find sessions by meaning, not just keywords.</p>
      <CodeBlock title="terminal">
        {`longhouse-server recall "how did I handle rate limiting"
longhouse-server recall "the session where CI was fixed"`}
      </CodeBlock>
      <p>
        Full-text keyword search is available through the browser timeline and
        the <code>/api/agents/sessions?query=...</code> API endpoint.
      </p>

      <h2>Session Control</h2>

      <h3>longhouse-server wall</h3>
      <p>List active and recent sessions, scoped to the current project or machine.</p>
      <CodeBlock title="terminal">
        {`longhouse-server wall                 # human-readable
longhouse-server wall --json          # machine-readable`}
      </CodeBlock>

      <h3>longhouse-server peers</h3>
      <p>Show connected peers and runner machines available for remote execution.</p>
      <CodeBlock title="terminal">
        {`longhouse-server peers`}
      </CodeBlock>

      <h3>longhouse-server tail</h3>
      <p>Stream live events from a running session.</p>
      <CodeBlock title="terminal">
        {`longhouse-server tail SESSION_ID`}
      </CodeBlock>

      <h3>longhouse-server message</h3>
      <p>Send a directed message to a session's inbox.</p>
      <CodeBlock title="terminal">
        {`longhouse-server message SESSION_ID "Check the failing test"
longhouse-server messages ack MESSAGE_ID  # acknowledge a message`}
      </CodeBlock>

      <h3>longhouse-server continue</h3>
      <p>Continue a stopped session from its recovered context.</p>
      <CodeBlock title="terminal">
        {`longhouse-server continue SESSION_ID`}
      </CodeBlock>

      <h3>longhouse-server sessions</h3>
      <p>Inspect session detail and events.</p>
      <CodeBlock title="terminal">
        {`longhouse-server sessions get SESSION_ID --json
longhouse-server sessions events SESSION_ID
longhouse-server sessions continue SESSION_ID`}
      </CodeBlock>

      <h3>longhouse auth</h3>
      <p>Store a device token from the environment for native device commands.</p>
      <CodeBlock title="terminal">{`LONGHOUSE_DEVICE_TOKEN="..." longhouse auth --url https://your-runtime.example`}</CodeBlock>

      <h2>Diagnostics</h2>

      <h3>longhouse machine</h3>
      <p>Install or repair the native Machine Agent service.</p>
      <CodeBlock title="terminal">
        {`longhouse local-health --fast --json
longhouse machine repair
longhouse machine repair --repair-service`}
      </CodeBlock>

      <h2>Install & Maintenance</h2>

      <h3>Native upgrades</h3>
      <p>Re-run the native installer to install the current paired binaries.</p>
      <CodeBlock title="terminal">
        {`curl -fsSL https://get.longhouse.ai/install.sh | bash
longhouse verify-pair`}
      </CodeBlock>

      <h2>Global Flags</h2>
      <table>
        <thead>
          <tr>
            <th>Flag</th>
            <th>Description</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><code>--json</code></td>
            <td>Machine-readable JSON output</td>
          </tr>
          <tr>
            <td><code>--port PORT</code></td>
            <td>Override the server port</td>
          </tr>
          <tr>
            <td><code>--limit N</code></td>
            <td>Limit result count</td>
          </tr>
          <tr>
            <td><code>--project NAME</code></td>
            <td>Scope to a specific project</td>
          </tr>
        </tbody>
      </table>
    </>
  );
}
