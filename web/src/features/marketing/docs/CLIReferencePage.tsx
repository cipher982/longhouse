import { Link } from "react-router";
import { usePageMeta } from "@/shared/hooks/usePageMeta";
import { getLaunchProviderSupportList } from "@/shared/lib/providers";
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
longhouse omp
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
        <code>opencode</code>, <code>pi</code>, <code>omp</code>, and{" "}
        <code>agy</code> runs still import into the timeline. They stay Shadow
        sessions: searchable and observable, with no control path, because
        Longhouse never owned one.
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
        command. Without a token in the environment it opens the Runtime Host
        in a browser to approve this machine. The URL defaults to the one
        stored on this machine, which the installer writes from{" "}
        <code>LONGHOUSE_URL</code>.
      </p>
      <CodeBlock title="terminal">
        {`longhouse auth
longhouse auth --url https://you.longhouse.ai
LONGHOUSE_DEVICE_TOKEN="..." longhouse auth --url https://you.longhouse.ai
longhouse auth --url http://192.168.1.20:8080 --allow-insecure-http
longhouse auth --clear`}
      </CodeBlock>
      <p>
        Plain <code>http://</code> is accepted only for loopback and Tailscale
        addresses. A LAN address needs <code>--allow-insecure-http</code> (or{" "}
        <code>LONGHOUSE_ALLOW_INSECURE_HTTP=1</code>), which is stored with that
        address and warns on every use; any other address needs{" "}
        <code>https://</code>. Every native client follows the same rule, as do the{" "}
        <code>longhouse-server</code> commands that send the device token.
      </p>
      <p>
        <code>--clear</code> disconnects the machine: it revokes this
        machine&apos;s device token on the Runtime Host, then deletes the stored
        credentials. If the Runtime Host cannot be reached the credentials are
        kept so you can retry; <code>--clear --local-only</code> deletes them
        anyway and leaves the token to be revoked under Settings → Devices.
      </p>

      <h3>longhouse machine</h3>
      <p>Install, repair, or restart the native Machine Agent service.</p>
      <CodeBlock title="terminal">
        {`longhouse machine repair
longhouse machine repair --dry-run
longhouse machine repair --repair-service`}
      </CodeBlock>

      <h3>longhouse machine scope</h3>
      <p>
        Choose which existing session history the Machine Agent may import.
        Old transcripts can hold code and secrets from any project you ever ran
        an agent in, so a newly connected machine that has not chosen imports
        only sessions that <em>start</em> from now on. A machine that was
        already shipping history before scopes existed keeps shipping all of
        it. Sessions you start later always ship. Changing the scope later
        backfills whatever became eligible.
      </p>
      <CodeBlock title="terminal">
        {`longhouse machine scope                      # show the scope and what it leaves out
longhouse machine scope --since now          # only new sessions (the default)
longhouse machine scope --since 2026-09-01   # sessions started on or after a date
longhouse machine scope --since all          # everything on this computer
longhouse machine scope --project ~/git/app  # also the full history of a folder
longhouse machine scope --no-projects        # drop the per-folder opt-ins
longhouse machine scope --prompt             # ask (needs a terminal)`}
      </CodeBlock>
      <p>
        A session belongs to a folder when the working directory it recorded is
        that folder or below it (Claude, Codex, OpenCode, Pi, OMP and Cursor
        record one; Antigravity history is covered by the date rule only).
        A session&apos;s start is its file&apos;s creation time (last
        modification where the file system keeps none), or the session&apos;s
        own first timestamp when that is earlier. Narrowing the scope stops
        further imports; sessions already uploaded stay on your Runtime Host.
        Naming a file yourself with <code>longhouse-server ship --file</code> is
        not filtered.
      </p>

      <h3>longhouse uninstall</h3>
      <p>
        Remove Longhouse from this computer: revoke this machine&apos;s device
        token on the Runtime Host, stop and remove the Machine Agent service,
        remove the hooks Longhouse added to Claude Code and Cursor, and delete
        the binaries (and <code>Longhouse.app</code> on a Mac). It stops before
        removing anything if the token cannot be revoked.
      </p>
      <CodeBlock title="terminal">
        {`longhouse uninstall --dry-run   # list what would be removed
longhouse uninstall             # asks first; --yes skips the question
longhouse uninstall --purge     # also delete local state under ~/.longhouse
longhouse uninstall --local-only  # do not revoke the token on the Runtime Host
longhouse uninstall --keep-app    # leave Longhouse.app in place (macOS)`}
      </CodeBlock>
      <p>
        Sessions already uploaded stay in your Runtime Host&apos;s archive until
        you delete them there. To cut off a machine you cannot reach, use{" "}
        <strong>Settings → Devices → Revoke</strong> on the Runtime Host.
      </p>

      <h3>longhouse local-health</h3>
      <p>
        The same snapshot the macOS menu bar shows: whether the machine is
        paired, whether the agent is running, and what it can see.
      </p>
      <CodeBlock title="terminal">
        {`longhouse local-health --json`}
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
        ask what history to import, and open the timeline.{" "}
        <code>--remote-url</code> points it at a Runtime Host you already run
        instead.
      </p>
      <CodeBlock title="terminal">
        {`longhouse-server onboard
longhouse-server onboard --remote-url https://you.longhouse.ai`}
      </CodeBlock>

      <h3>longhouse-server ship</h3>
      <p>
        One-shot import of the sessions already on disk that this machine&apos;s
        import scope allows (see <code>longhouse machine scope</code>). Useful
        for backfilling a machine before the Machine Agent takes over.
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
