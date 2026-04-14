import { usePageMeta } from "../../hooks/usePageMeta";
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
        Install Longhouse, open it, and find one prior session. That is the
        first proof it is already useful on this machine.
      </p>

      <h2>1. Install</h2>
      <p>Run the installer on macOS, Linux, or WSL:</p>
      <CodeBlock title="terminal">
        {`curl -fsSL https://get.longhouse.ai/install.sh | bash`}
      </CodeBlock>
      <p>
        On Apple Silicon Macs, you can also download <code>Longhouse.app</code>{" "}
        directly. The app download and terminal bootstrap install the same Mac
        product.
      </p>
      <p>
        The installer sets up the CLI and runs the default local quickstart. On
        macOS, it installs <code>Longhouse.app</code> in{" "}
        <code>/Applications</code> and uses it as the always-on local status
        surface. Green means local shipping is healthy; yellow or red means
        there is an issue to repair.
      </p>
      <p>
        Requires Python 3.12+. No sudo needed.
      </p>

      <h2>2. Open Longhouse</h2>
      <p>
        The quickstart starts Longhouse for you. On macOS, open{" "}
        <code>Longhouse.app</code> if it is not already open. On Linux or WSL,
        open <code>http://localhost:8080</code>. Your data stays in a SQLite
        database on your machine at <code>~/.longhouse/longhouse.db</code>.
      </p>
      <p>
        This laptop setup is the fast proof path. When you want Longhouse to
        stay reachable while the laptop sleeps, move the Runtime Host to a
        machine that stays on and keep the Machine Agent on the dev machine
        where work happens.
      </p>

      <h2>3. Find one prior session</h2>
      <p>
        Use the timeline or search to find one real past session. If you have
        used Claude Code, Codex, or Gemini CLI on this machine, Longhouse has
        already imported your sessions during onboarding.
      </p>
      <div className="docs-callout">
        <p>
          <strong>No sessions yet?</strong> Run{" "}
          <code>longhouse serve --demo</code> for a safe preview with synthetic
          data. Then import your real sessions when you are ready.
        </p>
      </div>
      <div className="docs-callout">
        <p>
          <strong>Imported runs are unmanaged.</strong> Longhouse still shows
          them in the timeline, but there is no live control path open. Treat
          bare CLI history as observe-only until you launch a managed session
          yourself.
        </p>
      </div>

      <h2>4. Launch a managed session</h2>
      <p>
        Bare provider CLIs create unmanaged sessions that are useful for quick
        local work but cannot accept browser messages. Start through Longhouse
        instead when you want a <strong>managed</strong> session to stay
        reachable later:
      </p>
      <CodeBlock title="terminal">
        {`longhouse claude    # Claude Code with control channel
longhouse codex     # Codex CLI with control channel`}
      </CodeBlock>
      <p>
        When Longhouse launches the session, it owns the control path so you
        can message or reattach later from the browser, CLI, or API. Claude is
        the strongest managed path today, but <code>longhouse codex</code> also keeps a
        Codex session steerable.
      </p>
      <div className="docs-callout">
        <p>
          <strong>Managed vs unmanaged.</strong> Both show up in the timeline,
          but only managed sessions keep a live control channel. Use
          <code>longhouse claude</code> or <code>longhouse codex</code> whenever you want to keep a
          session steerable.
        </p>
      </div>

      <h2>5. Troubleshooting</h2>
      <p>
        Most people should not need this on the first run. If the timeline or
        menu bar says something is wrong:
      </p>
      <CodeBlock title="terminal">
        {`longhouse doctor            # diagnose issues
longhouse connect --install  # repair onboarding and automatic imports`}
      </CodeBlock>
      <p>
        On macOS, <code>Longhouse.app</code> and the menu bar show the same
        local health information in ambient form.
      </p>
    </>
  );
}
