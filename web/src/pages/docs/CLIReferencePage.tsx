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

      <h3>longhouse claude / codex</h3>
      <p>
        Start a provider CLI session with a Longhouse control channel. The
        session runs in your terminal and stays reachable from other surfaces.
      </p>
      <CodeBlock title="terminal">
        {`longhouse claude               # start Claude Code
longhouse codex                # start Codex CLI`}
      </CodeBlock>

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

      <h3>longhouse tail</h3>
      <p>Stream live events from a running session.</p>
      <CodeBlock title="terminal">
        {`longhouse tail SESSION_ID`}
      </CodeBlock>

      <h3>longhouse message</h3>
      <p>Send a directed message to a session's inbox.</p>
      <CodeBlock title="terminal">
        {`longhouse message SESSION_ID "Check the failing test"
longhouse messages --json      # read the inbox`}
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

      <h2>Diagnostics</h2>

      <h3>longhouse doctor</h3>
      <p>Check for issues with the local installation.</p>
      <CodeBlock title="terminal">{`longhouse doctor`}</CodeBlock>

      <h3>longhouse connect</h3>
      <p>Manage the background session shipping service.</p>
      <CodeBlock title="terminal">
        {`longhouse connect --install     # repair onboarding
longhouse connect --status      # check shipping status`}
      </CodeBlock>

      <h3>longhouse local-health</h3>
      <p>Detailed local health check. On macOS, the menu bar app shows this in ambient form.</p>
      <CodeBlock title="terminal">{`longhouse local-health`}</CodeBlock>

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
