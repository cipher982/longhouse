import { Link } from "react-router-dom";
import { usePageMeta } from "../../hooks/usePageMeta";
import { getLaunchProviderSupportList } from "../../lib/providers";
import { CodeBlock } from "./CodeBlock";

const LAUNCH_PROVIDERS = getLaunchProviderSupportList();

function yesNo(value: boolean) {
  return value ? "Yes" : "No";
}

export default function CLIReferencePage() {
  usePageMeta({
    title: "CLI Reference - Longhouse Docs",
    description: "What the longhouse and longhouse-server commands actually do.",
  });

  return (
    <>
      <h1>CLI Reference</h1>
      <p className="docs-subtitle">
        Longhouse installs two binaries. <code>longhouse</code> is the native
        device CLI: it pairs the machine, launches managed provider sessions,
        and reports local health. <code>longhouse-server</code> runs and
        inspects the Runtime Host. Commands do not cross between them.
      </p>
      <div className="docs-callout">
        <p>
          Each binary prints its own list. <code>longhouse --help</code> and{" "}
          <code>longhouse-server --help</code> are the authority if this page
          ever drifts.
        </p>
      </div>

      <h2>Device CLI (<code>longhouse</code>)</h2>

      <h3>Managed provider sessions</h3>
      <p>
        Each of these launches the provider&apos;s own CLI in your terminal
        while Longhouse owns the control path, so the session stays reachable
        from the browser and the API after you walk away.
      </p>
      <CodeBlock title="terminal">
        {`longhouse claude
longhouse codex
longhouse cursor
longhouse opencode
longhouse pi --prompt "summarize the failing test"
longhouse antigravity`}
      </CodeBlock>
      <p>
        What each one can do afterwards is not uniform. The table below is
        generated from <code>schemas/managed_providers.yml</code>, the contract
        that also drives the runtime, so it cannot quietly disagree with the
        product.
      </p>
      <table>
        <thead>
          <tr>
            <th>Provider</th>
            <th>Command</th>
            <th>Send</th>
            <th>Interrupt</th>
            <th>Mid-turn steer</th>
            <th>Resume</th>
          </tr>
        </thead>
        <tbody>
          {LAUNCH_PROVIDERS.map((provider) => (
            <tr key={provider.id}>
              <td>{provider.marketingName}</td>
              <td>
                {provider.nativeLaunchCommand ? (
                  <code>{provider.nativeLaunchCommand}</code>
                ) : (
                  "—"
                )}
              </td>
              <td>{yesNo(provider.launchAndSend)}</td>
              <td>{yesNo(provider.interrupt)}</td>
              <td>{yesNo(provider.steerMidTurn)}</td>
              <td>{yesNo(provider.resume)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p>
        Bare <code>claude</code>, <code>codex</code>, <code>cursor-agent</code>,{" "}
        <code>opencode</code>, <code>pi</code>, and <code>agy</code> runs still
        import into the timeline. They stay Shadow sessions: searchable and
        observable, with no control path, because Longhouse never owned one.
      </p>
      <p>
        Codex and OpenCode also take <code>attach</code> and <code>stop</code>{" "}
        subcommands, which reattach to or shut down a running managed session.
        Claude and Cursor take <code>configure</code>, which installs their
        native Longhouse hooks.
      </p>

      <h3>longhouse auth</h3>
      <p>
        Store the device credential this machine uses for every other native
        command. The token comes from the environment, or from a browser
        pairing flow.
      </p>
      <CodeBlock title="terminal">
        {`LONGHOUSE_DEVICE_TOKEN="..." longhouse auth --url https://your-runtime.example
longhouse auth --url https://your-runtime.example --browser
longhouse auth --clear`}
      </CodeBlock>

      <h3>longhouse machine</h3>
      <p>Install, repair, or restart the native Machine Agent service.</p>
      <CodeBlock title="terminal">
        {`longhouse machine repair
longhouse machine repair --repair-service`}
      </CodeBlock>

      <h3>longhouse local-health</h3>
      <p>
        The same snapshot the macOS menu bar shows: whether the machine is
        paired, whether the agent is running, and what it can see.
      </p>
      <CodeBlock title="terminal">
        {`longhouse local-health --fast --json`}
      </CodeBlock>

      <h3>longhouse shipping</h3>
      <p>
        Inspect or discard the evidence retained for uploads that could not be
        shipped. <code>discard</code> does nothing without{" "}
        <code>--confirm</code>; it reports what it would remove.
      </p>
      <CodeBlock title="terminal">
        {`longhouse shipping inspect
longhouse shipping inspect --json
longhouse shipping discard --source-epoch EPOCH --confirm`}
      </CodeBlock>

      <h3>longhouse build-identity / verify-pair</h3>
      <p>
        The device CLI and the engine ship as a pair built from one commit.
        These print that identity and check the pairing held through install.
      </p>
      <CodeBlock title="terminal">
        {`longhouse build-identity --json
longhouse verify-pair`}
      </CodeBlock>

      <h2>Runtime Host (<code>longhouse-server</code>)</h2>

      <h3>longhouse-server serve</h3>
      <p>Start the Runtime Host from its server environment.</p>
      <CodeBlock title="terminal">
        {`longhouse-server serve                       # localhost:8080
longhouse-server serve --port 9090          # custom port
longhouse-server serve --demo               # start with sample data
longhouse-server serve --daemon             # run in background
longhouse-server serve --stop               # stop the background server`}
      </CodeBlock>
      <p>
        On a loopback bind auth is off for frictionless local use. On a public
        bind it is required — see{" "}
        <Link to="/docs/configuration">Configuration</Link>.
      </p>

      <h3>longhouse-server onboard</h3>
      <p>
        The guided path: start a local Runtime Host, install the Machine Agent,
        and open the timeline. <code>--remote-url</code> points it at a Runtime
        Host you already run instead.
      </p>
      <CodeBlock title="terminal">
        {`longhouse-server onboard
longhouse-server onboard --remote-url https://your-runtime.example`}
      </CodeBlock>

      <h3>longhouse-server ship</h3>
      <p>
        One-shot import of Claude Code sessions already on disk. Useful for
        backfilling a machine before the Machine Agent takes over.
      </p>
      <CodeBlock title="terminal">
        {`longhouse-server ship
longhouse-server ship --file path/to/session.jsonl`}
      </CodeBlock>

      <h3>longhouse-server recall</h3>
      <p>Search past sessions from the terminal.</p>
      <CodeBlock title="terminal">
        {`longhouse-server recall "how did I handle rate limiting"
longhouse-server recall "deploy fix" --project longhouse --days-back 30
longhouse-server recall-context REF   # REF comes back with each recall result`}
      </CodeBlock>

      <h3>longhouse-server sessions</h3>
      <p>Inspect a session, read its events, or interrupt its active turn.</p>
      <CodeBlock title="terminal">
        {`longhouse-server sessions get SESSION_ID --json
longhouse-server sessions events SESSION_ID --roles user,assistant
longhouse-server sessions interrupt SESSION_ID`}
      </CodeBlock>

      <h3>longhouse-server tail</h3>
      <p>
        Read the recent tail of a session. Tool output dominates most
        transcripts, so <code>--roles user,assistant</code> is usually what you
        want.
      </p>
      <CodeBlock title="terminal">
        {`longhouse-server tail SESSION_ID
longhouse-server tail SESSION_ID --roles user,assistant -n 50`}
      </CodeBlock>

      <h3>longhouse-server send / inbox / reply</h3>
      <p>
        Directed input between managed sessions. A send persists before any
        delivery attempt, so the target picks it up whenever it next reads its
        inbox.
      </p>
      <CodeBlock title="terminal">
        {`longhouse-server send SESSION_ID "Check the failing test in auth.py"
longhouse-server inbox
longhouse-server reply INPUT_ID "Fixed — it was the token clock skew"`}
      </CodeBlock>

      <h3>longhouse-server continue</h3>
      <p>Continue a session with a follow-up message. Both arguments are required.</p>
      <CodeBlock title="terminal">
        {`longhouse-server continue SESSION_ID "Now run the integration tests"`}
      </CodeBlock>

      <h3>longhouse-server peers</h3>
      <p>List other sessions working around the same repo.</p>
      <CodeBlock title="terminal">
        {`longhouse-server peers
longhouse-server peers --all --days 14`}
      </CodeBlock>

      <h3>longhouse-server status / config / db</h3>
      <p>
        Local health in one line, effective configuration with the source of
        each value, and SQLite diagnostics.
      </p>
      <CodeBlock title="terminal">
        {`longhouse-server status --verbose
longhouse-server config show
longhouse-server db doctor`}
      </CodeBlock>

      <h3>longhouse-server version / upgrade</h3>
      <CodeBlock title="terminal">
        {`longhouse-server version --check
longhouse-server upgrade`}
      </CodeBlock>
      <p>
        The device binaries upgrade separately, by re-running the installer:{" "}
        <code>curl -fsSL https://get.longhouse.ai/install.sh | bash</code>, then{" "}
        <code>longhouse verify-pair</code>.
      </p>

      <h2>Listing what is running</h2>
      <p>
        There is no <code>wall</code> subcommand on either binary. The wall is a
        query on the Machine API and the browser view over it — see{" "}
        <Link to="/docs/api">Machine API</Link>.
      </p>
      <CodeBlock title="terminal">
        {`curl "http://localhost:8080/api/agents/sessions/wall?project=longhouse&days=7"`}
      </CodeBlock>

      <h2>Common flags</h2>
      <table>
        <thead>
          <tr>
            <th>Flag</th>
            <th>Where</th>
            <th>Description</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td><code>--json</code> / <code>-j</code></td>
            <td>Most read commands on both binaries</td>
            <td>Machine-readable JSON instead of the formatted output</td>
          </tr>
          <tr>
            <td><code>--url</code> / <code>--token</code></td>
            <td><code>longhouse-server</code> API commands</td>
            <td>Override the stored Runtime Host URL and device token</td>
          </tr>
          <tr>
            <td><code>--limit</code> / <code>-n</code></td>
            <td><code>recall</code>, <code>tail</code>, <code>peers</code>, <code>sessions events</code>, <code>inbox</code></td>
            <td>Cap the number of results</td>
          </tr>
          <tr>
            <td><code>--project</code> / <code>-p</code></td>
            <td><code>recall</code></td>
            <td>Scope the search to one project</td>
          </tr>
          <tr>
            <td><code>--port</code> / <code>-p</code></td>
            <td><code>serve</code>, <code>onboard</code></td>
            <td>Override the Runtime Host port</td>
          </tr>
        </tbody>
      </table>
    </>
  );
}
