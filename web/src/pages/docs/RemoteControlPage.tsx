import { Link } from "react-router-dom";
import { usePageMeta } from "../../hooks/usePageMeta";
import { CodeBlock } from "./CodeBlock";

export default function RemoteControlPage() {
  usePageMeta({
    title: "Remote Control - Longhouse Docs",
    description: "Keep managed sessions steerable after launch.",
  });

  return (
    <>
      <h1>Control After Launch</h1>
      <p className="docs-subtitle">
        Managed sessions stay reachable after the terminal closes. Message
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
          <strong>Provider truth matters.</strong> Claude is the strongest
          control-after-launch path today. Codex is supported and useful here
          too. Antigravity is the new Google CLI path; existing Gemini sessions
          still land in the archive as legacy imports.
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
      <CodeBlock title="terminal">
        {`longhouse wall              # list active and recent sessions
longhouse wall --json       # machine-readable output`}
      </CodeBlock>

      <h3>Watch live events</h3>
      <CodeBlock title="terminal">
        {`longhouse tail SESSION_ID   # stream events as they happen`}
      </CodeBlock>

      <h3>Send a message</h3>
      <CodeBlock title="terminal">
        {`longhouse message SESSION_ID "Check the failing test in auth.py"`}
      </CodeBlock>
      <p>
        The message appears in the session's directed inbox. If the session is
        still running, it can pick up the message and act on it.
      </p>

      <h3>Continue later</h3>
      <p>
        When you come back to a session that has stopped, you can continue
        from the recovered context:
      </p>
      <CodeBlock title="terminal">
        {`longhouse continue SESSION_ID`}
      </CodeBlock>
      <p>
        This works from the browser too — open the session detail page and use
        the continue action.
      </p>

      <h2>Browser and CLI stay in sync</h2>
      <p>
        The timeline, session detail, wall, tail, and message commands all point
        at the same session surface. Actions you take in the browser are visible
        from the CLI and vice versa. There is no separate "browser session" or
        "CLI session" — there is one session with multiple ways to reach it.
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
            <td><code>longhouse claude</code> + <code>longhouse wall</code></td>
          </tr>
        </tbody>
      </table>
      <div className="docs-callout">
        <p>
          <strong>Bare provider CLIs still import.</strong> That compatibility
          path exists so Longhouse is useful on day one, not because it is the
          recommended steady state. Managed sessions launched through Longhouse
          keep the control channel open so you can message them, tail them, or
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
