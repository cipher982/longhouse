import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { ZapIcon, SearchIcon, SettingsIcon, MessageCircleIcon } from "../components/icons";
import { usePageMeta } from "../hooks/usePageMeta";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import "../styles/info-pages.css";

export default function DocsPage() {
  const currentYear = new Date().getFullYear();

  usePublicPageScroll();
  usePageMeta({
    title: "Documentation - Longhouse",
    description:
      "Learn how to use Longhouse. Quick start guides, timeline search tips, and setup instructions for Claude Code, Codex CLI, and Gemini CLI.",
  });

  return (
    <div className="info-page">
      <header className="info-page-header">
        <div className="info-page-header-inner">
          <Link to="/" className="info-page-back">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M19 12H5M12 19l-7-7 7-7" />
            </svg>
            Back to Home
          </Link>
          <Link to="/" className="info-page-brand">
            <SwarmLogo size={28} />
            <span className="info-page-brand-name">Longhouse</span>
          </Link>
        </div>
      </header>

      <main className="info-page-content">
        <h1 className="info-page-title">Documentation</h1>
        <p className="info-page-subtitle">
          Get started fast, bring sessions into one timeline, then start live control or cloud branches from synced thread context when you need them.
        </p>

        <nav className="docs-nav">
          <a href="#quickstart" className="docs-nav-card">
            <ZapIcon width={32} height={32} className="docs-nav-icon" />
            <h3>Quick Start</h3>
            <p>Install and run in 2 minutes</p>
          </a>
          <a href="#search" className="docs-nav-card">
            <SearchIcon width={32} height={32} className="docs-nav-icon" />
            <h3>Search</h3>
            <p>Find any session instantly</p>
          </a>
          <a href="#agents" className="docs-nav-card">
            <SettingsIcon width={32} height={32} className="docs-nav-icon" />
            <h3>Supported Agents</h3>
            <p>Claude Code, Codex, Gemini</p>
          </a>
        </nav>

        <section id="quickstart" className="docs-section">
          <h2>Quick Start</h2>

          <h3>1. Install</h3>
          <p>
            Run the installer on macOS, Linux, or WSL:
          </p>
          <pre><code>curl -fsSL https://get.longhouse.ai/install.sh | bash</code></pre>
          <p>
            The installer sets up the CLI and runs guided onboarding. The default path wires the local
            runtime too: engine, hooks, and the macOS status menu when available. Requires Python 3.12+.
            No sudo needed.
          </p>

          <h3>2. Start Longhouse</h3>
          <pre><code>longhouse serve</code></pre>
          <p>
            Opens a local web UI at <code>http://localhost:8080</code>.
            Data is stored in a SQLite database on your machine.
          </p>

          <h3>3. Repair or reinstall the local runtime</h3>
          <p>
            If you skipped onboarding or want to reinstall the engine, hooks, or ambient macOS helper, run:
          </p>
          <pre><code>longhouse connect --install
longhouse local-health</code></pre>
          <p>
            The runtime repair step is idempotent. After that, keep using Claude Code, Codex CLI, or
            Gemini CLI as normal. Longhouse imports their sessions into the timeline so you can browse,
            search, and inspect them from one place.
          </p>

          <h3>4. Start through Longhouse when you want live control</h3>
          <p>
            All sessions live in the same timeline. Starting through Longhouse adds an explicit control
            capability later:
          </p>
          <pre><code>longhouse claude
longhouse codex</code></pre>
          <p>
            Claude is the strongest cloud-branch path today. Codex and Gemini are already useful in the
            archive, machine surface, and managed-local control path.
          </p>

          <h3>5. Optional: start future sessions through Longhouse by habit</h3>
          <p>
            Default install stays non-invasive. The cleanest prelaunch habit is to start control-ready
            sessions explicitly:
          </p>
          <pre><code>longhouse claude
longhouse codex</code></pre>
          <p>
            Keep using bare provider CLIs when you only want local work. Use the Longhouse launch commands
            when you want the session to stay reachable from the timeline later.
          </p>

          <h3>6. Want a safe preview first?</h3>
          <p>
            Only use demo data when you want a safe preview before importing real sessions:
          </p>
          <pre><code>longhouse serve --demo</code></pre>
          <p>
            Use this only when you want a safe preview before importing real work.
          </p>
        </section>

        <section id="search" className="docs-section">
          <h2>Search</h2>
          <p>
            Longhouse provides full-text search across your AI coding sessions.
            Search by keyword, file name, tool name, project, or any text from your conversations.
          </p>

          <h3>What&apos;s indexed</h3>
          <ul>
            <li>Conversation messages (user and assistant)</li>
            <li>Tool calls and their outputs (file edits, bash commands, etc.)</li>
            <li>Session metadata (project, branch, timestamps)</li>
          </ul>

          <h3>Tips</h3>
          <ul>
            <li>Search for file names to find sessions that touched specific code</li>
            <li>Search for error messages to find how you solved similar issues</li>
            <li>Use the timeline filters to narrow by date range or provider</li>
          </ul>
        </section>

        <section id="agents" className="docs-section">
          <h2>Supported Agents</h2>
          <p>
            Longhouse reads the session files these tools already produce. The installer and onboarding flow handle the normal shipping setup for supported local CLIs.
          </p>

          <h3>Current Support</h3>
          <ul>
            <li><strong>Claude Code</strong> — strongest cloud-branch path today: timeline sync, hooks, and direct browser branching</li>
            <li><strong>Codex CLI</strong> — timeline sync, search/detail, and control-ready launches through Longhouse today; direct cloud branching is not supported yet</li>
            <li><strong>Gemini CLI</strong> — timeline sync and search/detail today; direct cloud branching is not supported yet</li>
          </ul>

          <h3>Coming Soon</h3>
          <ul>
            <li><strong>OpenCode</strong> — timeline import and hosted workflows</li>
            <li><strong>Cursor</strong> — IDE-integrated AI sessions</li>
          </ul>

          <h3>How it works</h3>
          <p>
            Longhouse watches for new session files and imports them
            into the local SQLite database. Sessions are deduplicated by ID, so
            re-importing is safe and idempotent.
          </p>
        </section>

        <section id="config" className="docs-section">
          <h2>Configuration</h2>

          <h3>Authentication</h3>
          <p>
            For local-only quickstarts, auth is disabled by default. To add password protection:
          </p>
          <pre><code>LONGHOUSE_PASSWORD=your-password longhouse serve</code></pre>
          <p>
            Before binding beyond localhost, set <code>LONGHOUSE_PASSWORD</code> or <code>LONGHOUSE_PASSWORD_HASH</code>.
          </p>

          <h3>Port</h3>
          <p>
            Default port is 8080. Override with:
          </p>
          <pre><code>longhouse serve --port 8081</code></pre>

          <h3>Data location</h3>
          <p>
            The SQLite database is stored at <code>~/.longhouse/longhouse.db</code> by default.
            Override with the <code>DATABASE_URL</code> environment variable.
          </p>
        </section>

        <section className="docs-section">
          <h2>Need Help?</h2>
          <p>
            <MessageCircleIcon width={16} height={16} className="docs-inline-icon" />
            Questions? Join our <a href="https://discord.gg/h2CWBUrj" target="_blank" rel="noopener noreferrer">Discord</a>,
            email <a href="mailto:support@longhouse.ai">support@longhouse.ai</a>, or
            open an issue on <a href="https://github.com/cipher982/longhouse" target="_blank" rel="noopener noreferrer">GitHub</a>.
          </p>
        </section>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Longhouse. All rights reserved.</p>
      </footer>
    </div>
  );
}
