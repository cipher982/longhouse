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
        The Longhouse CLI is the primary interface for starting sessions,
        searching history, and controlling live work.
      </p>

      <h2>Core Commands</h2>

      <h3>longhouse serve</h3>
      <p>Start the Longhouse server.</p>
      <CodeBlock title="terminal">
        {`longhouse serve                # default: localhost:8080
longhouse serve --port 9090    # custom port
longhouse serve --demo         # start with demo data`}
      </CodeBlock>

      <h3>longhouse claude / codex / antigravity / opencode</h3>
      <p>
        Launch a managed provider CLI session with a Longhouse control channel.
        The session runs in your terminal and stays reachable from other
        surfaces.
      </p>
      <CodeBlock title="terminal">
        {`longhouse claude               # start Claude Code (steerable)
longhouse codex                # start Codex CLI (steerable)
longhouse antigravity          # start Antigravity CLI (managed observe-only)
longhouse opencode             # start OpenCode (managed observe-only)`}
      </CodeBlock>
      <p>
        Use these as the default launch path for new work. Bare{" "}
        <code>claude</code>, <code>codex</code>, <code>antigravity</code>, and{" "}
        <code>opencode</code> runs still import into the timeline, but they
        remain unmanaged history. Claude and Codex support full live control;{" "}
        Antigravity and OpenCode are managed observe-only today (archive,
        transcript, and phase signals without remote send controls).
      </p>

      <h3>longhouse ship</h3>
      <p>
        One-time import of existing session files into the timeline. Useful
        when you want to backfill history from a machine.
      </p>
      <CodeBlock title="terminal">
        {`longhouse ship                 # import all detected sessions`}
      </CodeBlock>

      <h2>Search & Recall</h2>

      <h3>longhouse recall</h3>
      <p>Semantic search — find sessions by meaning, not just keywords.</p>
      <CodeBlock title="terminal">
        {`longhouse recall "how did I handle rate limiting"
longhouse recall "the session where CI was fixed"`}
      </CodeBlock>
      <p>
        Full-text keyword search is available through the browser timeline and
        the <code>/api/agents/sessions?query=...</code> API endpoint.
      </p>

      <h2>Session Control</h2>

      <h3>longhouse wall</h3>
      <p>List active and recent sessions, scoped to the current project or machine.</p>
      <CodeBlock title="terminal">
        {`longhouse wall                 # human-readable
longhouse wall --json          # machine-readable`}
      </CodeBlock>

      <h3>longhouse peers</h3>
      <p>Show connected peers and runner machines available for remote execution.</p>
      <CodeBlock title="terminal">
        {`longhouse peers`}
      </CodeBlock>

      <h3>longhouse tail</h3>
      <p>Stream live events from a running session.</p>
      <CodeBlock title="terminal">
        {`longhouse tail SESSION_ID`}
      </CodeBlock>

      <h3>longhouse message</h3>
      <p>Send a directed message to a session's inbox.</p>
      <CodeBlock title="terminal">
        {`longhouse message SESSION_ID "Check the failing test"
longhouse messages ack MESSAGE_ID  # acknowledge a message`}
      </CodeBlock>

      <h3>longhouse continue</h3>
      <p>Continue a stopped session from its recovered context.</p>
      <CodeBlock title="terminal">
        {`longhouse continue SESSION_ID`}
      </CodeBlock>

      <h3>longhouse sessions</h3>
      <p>Inspect session detail and events.</p>
      <CodeBlock title="terminal">
        {`longhouse sessions get SESSION_ID --json
longhouse sessions events SESSION_ID
longhouse sessions continue SESSION_ID`}
      </CodeBlock>

      <h2>Configuration</h2>

      <h3>longhouse config show</h3>
      <p>Display the current configuration and defaults.</p>
      <CodeBlock title="terminal">{`longhouse config show`}</CodeBlock>

      <h3>longhouse auth</h3>
      <p>Manage authentication — login, logout, token refresh.</p>
      <CodeBlock title="terminal">{`longhouse auth`}</CodeBlock>

      <h2>Diagnostics</h2>

      <h3>longhouse status</h3>
      <p>Show server status and connectivity information.</p>
      <CodeBlock title="terminal">{`longhouse status`}</CodeBlock>

      <h3>longhouse doctor</h3>
      <p>Diagnose common issues with the local installation and environment.</p>
      <CodeBlock title="terminal">{`longhouse doctor`}</CodeBlock>

      <h3>longhouse connect</h3>
      <p>Manage the background session shipping service.</p>
      <CodeBlock title="terminal">
        {`longhouse connect --install     # first install / link this machine
longhouse connect --status      # check shipping status`}
      </CodeBlock>

      <h3>longhouse machine</h3>
      <p>Repair or reconfigure an already-linked machine.</p>
      <CodeBlock title="terminal">
        {`longhouse machine repair                       # repair local runtime + replay backlog
longhouse machine configure --machine-name my-vps  # update canonical machine config`}
      </CodeBlock>

      <h2>Setup & Maintenance</h2>

      <h3>longhouse onboard</h3>
      <p>Run the default local quickstart on this machine.</p>
      <CodeBlock title="terminal">{`longhouse onboard`}</CodeBlock>

      <h3>longhouse migrate</h3>
      <p>Plan or apply heavy SQLite migrations. Useful after major version updates.</p>
      <CodeBlock title="terminal">
        {`longhouse migrate              # show pending migrations
longhouse migrate --apply       # apply pending migrations
longhouse migrate --json        # machine-readable output`}
      </CodeBlock>

      <h3>longhouse version / upgrade</h3>
      <p>Check or update your Longhouse installation.</p>
      <CodeBlock title="terminal">
        {`longhouse version              # show current version
longhouse version --check       # check for updates
longhouse upgrade               # upgrade to latest version`}
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
