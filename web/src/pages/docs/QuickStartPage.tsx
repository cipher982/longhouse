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
        The installer sets up the CLI and runs guided onboarding. On macOS,
        Longhouse also installs a menu bar app — the always-on local status
        surface. Green means local shipping is healthy; yellow or red means
        there is an issue to repair.
      </p>
      <p>
        Requires Python 3.12+. No sudo needed.
      </p>

      <h2>2. Open Longhouse</h2>
      <CodeBlock title="terminal">{`longhouse serve`}</CodeBlock>
      <p>
        Open <code>http://localhost:8080</code>. Your data stays in a SQLite
        database on your machine at <code>~/.longhouse/longhouse.db</code>.
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

      <h2>4. Start through Longhouse</h2>
      <p>
        Keep using bare provider CLIs when you only need local work. Start
        through Longhouse when you want the session to stay reachable later:
      </p>
      <CodeBlock title="terminal">
        {`longhouse claude    # Claude Code with control channel
longhouse codex     # Codex CLI with control channel`}
      </CodeBlock>
      <p>
        When Longhouse is in the launch path, the session stays addressable from
        the browser, CLI, or API after the terminal closes. Claude is the
        strongest control-ready path today.
      </p>

      <h2>5. Troubleshooting</h2>
      <p>
        Most people should not need this on the first run. If something looks
        wrong:
      </p>
      <CodeBlock title="terminal">
        {`longhouse doctor            # diagnose issues
longhouse connect --install  # repair onboarding and automatic imports
longhouse local-health       # check local status`}
      </CodeBlock>
      <p>
        On macOS, the menu bar app shows the same health information in ambient
        form.
      </p>
    </>
  );
}
