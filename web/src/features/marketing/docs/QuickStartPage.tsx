import { Link } from "react-router";
import { usePageMeta } from "@/shared/hooks/usePageMeta";
import { CodeBlock } from "./CodeBlock";

export default function QuickStartPage() {
  usePageMeta({
    title: "Quick Start - Longhouse Docs",
    description: "Install Longhouse and find your first session in under two minutes.",
  });

  return (
    <>
      <h1>Quick Start</h1>
      <p className="docs-subtitle">
        Install Longhouse, open it, and find one of your own sessions. That is
        the first proof it is already useful on this machine.
      </p>

      <h2>0. Get a Longhouse address</h2>
      <p>
        Connecting a machine needs a Longhouse to connect to. A hosted one is
        invite-only for now; to run your own on this machine (Linux or macOS):
      </p>
      <CodeBlock title="terminal">
        {`curl -LsSf https://astral.sh/uv/install.sh | sh   # skip if you have uv
uv tool install longhouse
longhouse-server onboard`}
      </CodeBlock>
      <p>
        That starts a Runtime Host at <code>http://127.0.0.1:8080</code>,
        installs the Machine Agent, and asks what history to import, so you can
        skip to step 4. It stops when the machine does; to keep one running,
        see <Link to="/docs/configuration">Configuration</Link>.
      </p>

      <h3>Your own always-on box, over Tailscale</h3>
      <p>
        If the box and your laptop share a tailnet, you do not need a domain or
        https. Tailscale already encrypts the link, so native clients accept
        plain <code>http://</code> to a Tailscale address (<code>100.x.y.z</code>,
        an <code>fd7a:115c:a1e0::</code> address, or a <code>*.ts.net</code>{" "}
        name). Start the Runtime Host on the box; it prints a{" "}
        <code>Tailscale:</code> address:
      </p>
      <CodeBlock title="on the always-on box">
        {`longhouse-server serve --host 0.0.0.0`}
      </CodeBlock>
      <CodeBlock title="on each dev machine">
        {`longhouse auth --url http://100.x.y.z:8080     # or http://my-box.your-tailnet.ts.net:8080
longhouse machine repair --repair-service`}
      </CodeBlock>
      <p>
        The macOS app and the iPhone app follow the same rule. A plain LAN
        address (<code>192.168.x</code>, <code>10.x</code>, <code>172.16-31.x</code>,{" "}
        <code>*.local</code>) is refused unless you opt in with{" "}
        <code>--allow-insecure-http</code> (or{" "}
        <code>LONGHOUSE_ALLOW_INSECURE_HTTP=1</code>; in the iPhone app, the
        switch in the server settings). The choice is remembered with the
        address and every use warns, because the device token and every
        transcript then cross that network unencrypted. Any other address needs{" "}
        <code>https://</code>.
      </p>

      <h2>1. Connect a machine</h2>
      <p>
        Run this on the machine where you use Claude Code, Codex, or another
        coding agent (macOS, Linux, or WSL), with your Longhouse address in
        place of the example. An empty timeline shows this line with its own
        address filled in.
      </p>
      <CodeBlock title="terminal">
        {`curl -fsSL https://get.longhouse.ai/install.sh | LONGHOUSE_URL=https://you.longhouse.ai bash`}
      </CodeBlock>
      <p>
        It installs Longhouse (on a Mac, also <code>Longhouse.app</code>), opens
        your Longhouse in the browser to approve the machine, and starts its
        Machine Agent. No Python runtime and no sudo needed.
      </p>
      <p>
        No browser on that machine, such as a server over SSH? In{" "}
        <strong>Settings → Devices</strong>, create a token; the page gives you
        one line that connects the server without a browser.
      </p>

      <h2>2. Finish later</h2>
      <p>
        If you skipped the approval or installed without an address, open{" "}
        <code>Longhouse.app</code> on a Mac and choose{" "}
        <strong>Sign in to connect this Mac</strong>. Anywhere else:
      </p>
      <CodeBlock title="terminal">
        {`longhouse auth --url https://you.longhouse.ai
longhouse machine repair --repair-service`}
      </CodeBlock>
      <p>
        This laptop setup is the fast proof path. When you want Longhouse to
        stay reachable while the laptop sleeps, move the Runtime Host to a
        machine that stays on and keep the Machine Agent on the dev machine
        where work happens.
      </p>

      <h2>3. Choose what history to import</h2>
      <p>
        Setup asks what to do with sessions your agents already saved on this
        computer. Old transcripts can hold code and secrets from any project you
        ever ran an agent in, so the default imports <strong>nothing old</strong>:
        only sessions you start from now on. Import history when you want it,
        for one project or for everything:
      </p>
      <CodeBlock title="terminal">
        {`longhouse machine scope                        # what is imported, what is left out
longhouse machine scope --project ~/git/app    # add one project's full history
longhouse machine scope --since all            # import everything on this computer`}
      </CodeBlock>
      <p>
        Changing the scope backfills what became eligible; nothing older than
        your choice is uploaded until then. To remove Longhouse from a computer
        (and revoke its token), run <code>longhouse uninstall</code>.
      </p>

      <h2>4. Find a session</h2>
      <p>
        Start a session in Claude Code, Codex, Cursor Agent, OpenCode, Pi, Oh My
        Pi, or Antigravity, then look for it in the timeline or search. A
        session you start after setup appears within seconds.
      </p>
      <div className="docs-callout">
        <p>
          <strong>No sessions yet?</strong> Run{" "}
          <code>uv tool install longhouse && longhouse-server serve --demo</code>{" "}
          for a safe preview.
        </p>
      </div>
      <div className="docs-callout">
        <p>
          <strong>Imported runs are unmanaged.</strong> Longhouse still shows
          them in the timeline, but there is no live control path open. Treat
          bare CLI history as observe-only, then restart the work through
          Longhouse when you want to keep it steerable.
        </p>
      </div>

      <h2>5. Launch a managed session</h2>
      <p>
        Bare provider CLIs are useful for compatibility import, but they are
        not the default path once Longhouse is installed. Start through
        Longhouse when you want a <strong>managed</strong> session to stay
        reachable later:
      </p>
      <CodeBlock title="terminal">
        {`longhouse claude       # Claude Code
longhouse codex        # Codex CLI
longhouse cursor       # Cursor Agent
longhouse opencode     # OpenCode
longhouse pi           # Pi Agent
longhouse omp          # Oh My Pi
longhouse antigravity  # Antigravity CLI, send only`}
      </CodeBlock>
      <p>
        When Longhouse launches the session, it owns the session record and
        local observation path. All seven providers ship today; what they can do
        after launch differs. Every provider but Antigravity can be steered
        mid-turn. OpenCode Helm supports managed send, interrupt, terminate,
        pause-answer, and active-turn steer that lands at the next step
        boundary. Antigravity takes send alone, and refuses to start if its
        Longhouse hook is not installed. The{" "}
        <Link to="/docs/integrations">Integrations</Link> page carries the full
        provider detail, generated from the managed-provider declarations.
      </p>
      <div className="docs-callout">
        <p>
          <strong>Managed vs unmanaged.</strong> Both show up in the timeline,
          but managed sessions keep Longhouse ownership of the launch and
          observation path. Launch through <code>longhouse &lt;provider&gt;</code>{" "}
          when you want to redirect a turn that is already running; a bare
          provider run stays observe-only.
        </p>
      </div>

      <h2>6. Troubleshooting</h2>
      <p>
        Most people should not need this on the first run. If the timeline or
        menu bar says something is wrong:
      </p>
      <CodeBlock title="terminal">
        {`longhouse local-health --json
longhouse machine repair
longhouse machine repair --repair-service`}
      </CodeBlock>
      <p>
        On macOS, <code>Longhouse.app</code> and the menu bar show the same
        local health information in ambient form.
      </p>
    </>
  );
}
