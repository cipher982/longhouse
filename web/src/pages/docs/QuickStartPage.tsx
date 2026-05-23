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
        The installer only acquires Longhouse. On macOS, it installs{" "}
        <code>Longhouse.app</code> in <code>/Applications</code>. On Linux or
        WSL, it installs the CLI. Setup is the next step, not part of the
        installer itself.
      </p>
      <p>
        Requires Python 3.12+. No sudo needed.
      </p>

      <h2>2. Open Longhouse</h2>
      <p>
        On macOS, open <code>Longhouse.app</code> to finish setup. On Linux or
        WSL, run <code>longhouse onboard</code>, then open{" "}
        <code>http://localhost:8080</code>. Your data stays in a SQLite
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
        used Claude Code, Codex, Antigravity, OpenCode, or legacy Gemini CLI on this machine, Longhouse will
        import your sessions during setup.
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
          bare CLI history as observe-only, then restart the work through
          Longhouse when you want to keep it steerable.
        </p>
      </div>

      <h2>4. Launch a managed session</h2>
      <p>
        Bare provider CLIs are useful for compatibility import, but they are
        not the default path once Longhouse is installed. Start through
        Longhouse when you want a <strong>managed</strong> session to stay
        reachable later:
      </p>
      <CodeBlock title="terminal">
        {`longhouse claude       # Claude Code with control channel
longhouse codex        # Codex CLI with control channel
longhouse antigravity  # Antigravity CLI, managed observe-only
longhouse opencode     # OpenCode, managed observe-only`}
      </CodeBlock>
      <p>
        When Longhouse launches the session, it owns the session record and
        local observation path. Claude is the strongest managed path today, and{" "}
        <code>longhouse codex</code> also keeps a Codex session steerable.
        Antigravity and OpenCode start as managed observe-only: archive,
        transcript, and phase signals without remote send controls yet.
      </p>
      <div className="docs-callout">
        <p>
          <strong>Managed vs unmanaged.</strong> Both show up in the timeline,
          but managed sessions keep Longhouse ownership of the launch and
          observation path. Use <code>longhouse claude</code> or{" "}
          <code>longhouse codex</code> for steerable sessions, and{" "}
          <code>longhouse antigravity</code> or <code>longhouse opencode</code>{" "}
          for managed observe-only archive and signals.
        </p>
      </div>

      <h2>5. Troubleshooting</h2>
      <p>
        Most people should not need this on the first run. If the timeline or
        menu bar says something is wrong:
      </p>
      <CodeBlock title="terminal">
        {`longhouse doctor            # diagnose issues
longhouse machine repair    # repair an already-linked machine
longhouse connect --install # first install or force reinstall`}
      </CodeBlock>
      <p>
        On macOS, <code>Longhouse.app</code> and the menu bar show the same
        local health information in ambient form.
      </p>
    </>
  );
}
