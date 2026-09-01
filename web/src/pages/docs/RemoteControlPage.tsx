import { Link } from "react-router-dom";
import { usePageMeta } from "../../hooks/usePageMeta";
import { getLaunchProviderSupportList } from "../../lib/providers";
import { CodeBlock } from "./CodeBlock";

const PROVIDERS = getLaunchProviderSupportList();
const STEERABLE = PROVIDERS.filter((p) => p.steerMidTurn);
const INTERRUPTIBLE = PROVIDERS.filter((p) => p.interrupt && !p.steerMidTurn);
const SEND_ONLY = PROVIDERS.filter((p) => p.launchAndSend && !p.interrupt);

export default function RemoteControlPage() {
  usePageMeta({
    title: "Remote Control - Longhouse Docs",
    description: "Keep managed sessions steerable after launch.",
  });

  return (
    <>
      <h1>Control After Launch</h1>
      <p className="docs-subtitle">
        Managed sessions stay reachable after the terminal closes. Send to
        them, tail them, or continue them later from the browser, CLI, or API.
      </p>
      <div className="docs-callout">
        <p>
          <strong>Managed vs unmanaged.</strong> Sessions started with bare
          provider CLIs are imported as unmanaged history—they land in the
          timeline but Longhouse does not own their live control channel. Use{" "}
          <code>longhouse claude</code> or <code>longhouse codex</code> as the
          normal launch path when you want a session to stay steerable long
          after the terminal closes.
        </p>
      </div>
      <div className="docs-callout">
        <p>
          <strong>Provider truth matters.</strong> Only{" "}
          {STEERABLE.map((p) => p.marketingName).join(" and ")} can be steered
          mid-turn. {INTERRUPTIBLE.map((p) => p.marketingName).join(", ")} take
          send and interrupt but not mid-turn steer, and{" "}
          {SEND_ONLY.map((p) => p.marketingName).join(", ")} takes send alone.
          The timeline offers each session only the controls its provider can
          actually perform.
        </p>
      </div>

      <h2>How it works</h2>
      <p>
        A bare <code>claude</code> or <code>codex</code> command runs a
        session that is only reachable in the terminal where you started it.
        When you launch a managed session through Longhouse instead, Longhouse keeps a control
        channel open alongside the session:
      </p>
      <CodeBlock title="terminal">
        {`longhouse claude    # starts Claude Code with control channel
longhouse codex     # starts Codex CLI with control channel`}
      </CodeBlock>
      <p>
        The session still runs in your terminal. The difference is that
        Longhouse can reach it later — from another terminal, the browser, or
        the API. One session, one execution owner, but multiple surfaces to
        observe and interact with it.
      </p>

      <h2>What you can do with a control channel</h2>

      <h3>See what is running</h3>
      <p>
        The timeline is the primary view. The same data is one query on the
        Machine API, which is what scripts should use — there is no{" "}
        <code>wall</code> subcommand on either binary.
      </p>
      <CodeBlock title="terminal">
        {`curl "http://localhost:8080/api/agents/sessions/wall?days=7"`}
      </CodeBlock>

      <h3>Watch recent events</h3>
      <CodeBlock title="terminal">
        {`longhouse-server tail SESSION_ID --roles user,assistant`}
      </CodeBlock>

      <h3>Send a message</h3>
      <CodeBlock title="terminal">
        {`longhouse-server send SESSION_ID "Check the failing test in auth.py"`}
      </CodeBlock>
      <p>
        The message lands in the session&apos;s directed inbox and is persisted
        before any delivery attempt. A running session picks it up and acts on
        it; a stopped one still has it waiting.
      </p>

      <h3>Continue later</h3>
      <p>
        When you come back to a session that has stopped, continue it with the
        follow-up you want it to pick up:
      </p>
      <CodeBlock title="terminal">
        {`longhouse-server continue SESSION_ID "Now run the integration tests"`}
      </CodeBlock>
      <p>
        This works from the browser too — open the session detail page and use
        the continue action.
      </p>

      <h2>Browser and CLI stay in sync</h2>
      <p>
        The timeline, session detail, wall query, tail, and send all point at
        the same session surface. Actions you take in the browser are visible
        from the CLI and vice versa. There is no separate &quot;browser
        session&quot; or &quot;CLI session&quot; — there is one session with
        multiple ways to reach it.
      </p>

      <h2>Which command should you start with?</h2>
      <table>
        <thead>
          <tr>
            <th>Situation</th>
            <th>Command</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>Quick local task you still may want to inspect later</td>
            <td><code>longhouse claude</code> / <code>longhouse codex</code></td>
          </tr>
          <tr>
            <td>Long-running work you want to check on later</td>
            <td><code>longhouse claude</code> / <code>longhouse codex</code></td>
          </tr>
          <tr>
            <td>Work on a remote machine you want to steer from your laptop</td>
            <td><code>longhouse claude</code> / <code>longhouse codex</code></td>
          </tr>
          <tr>
            <td>Coordinating multiple sessions on the same project</td>
            <td><code>longhouse claude</code> + <code>longhouse-server peers</code></td>
          </tr>
        </tbody>
      </table>
      <div className="docs-callout">
        <p>
          <strong>Bare provider CLIs still import.</strong> That compatibility
          path exists so Longhouse is useful on day one, not because it is the
          recommended steady state. Managed sessions launched through Longhouse
          keep the control channel open so you can send to them, tail them, or
          continue them from any surface.
        </p>
      </div>

      <p>
        For the full list of CLI commands, see the{" "}
        <Link to="/docs/cli">CLI Reference</Link>. For the HTTP endpoints
        behind these commands, see the{" "}
        <Link to="/docs/api">Machine API</Link>.
      </p>
    </>
  );
}
