import { Link } from "react-router-dom";
import { usePageMeta } from "../../hooks/usePageMeta";
import { CodeBlock } from "./CodeBlock";

export default function RemoteControlPage() {
  usePageMeta({
    title: "Remote Control - Longhouse Docs",
    description: "Keep a control channel open on sessions started through Longhouse.",
  });

  return (
    <>
      <h1>Control After Launch</h1>
      <p className="docs-subtitle">
        When you start a session through Longhouse, it stays reachable after the
        terminal closes. Message it, tail it, or continue it later — from the
        browser, CLI, or API.
      </p>

      <h2>How it works</h2>
      <p>
        A bare <code>claude</code> or <code>codex</code> command runs a
        session that is only reachable in the terminal where you started it.
        When you start through Longhouse instead, Longhouse keeps a control
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

      <h2>When to use bare CLI vs. Longhouse</h2>
      <table>
        <thead>
          <tr>
            <th>Situation</th>
            <th>Command</th>
          </tr>
        </thead>
        <tbody>
          <tr>
            <td>Quick local task, no need to come back</td>
            <td><code>claude</code> / <code>codex</code></td>
          </tr>
          <tr>
            <td>Long-running work you want to check on later</td>
            <td><code>longhouse claude</code></td>
          </tr>
          <tr>
            <td>Work on a remote machine you want to steer from your laptop</td>
            <td><code>longhouse claude</code></td>
          </tr>
          <tr>
            <td>Coordinating multiple sessions on the same project</td>
            <td><code>longhouse claude</code> + <code>longhouse wall</code></td>
          </tr>
        </tbody>
      </table>
      <div className="docs-callout">
        <p>
          <strong>Both paths land in the timeline.</strong> Sessions started
          with bare CLIs still get imported and searchable. Longhouse in the
          launch path adds the control channel — it does not create a different
          kind of session.
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
