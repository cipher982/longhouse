import { useEffect } from "react";
import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import { ZapIcon, SearchIcon, SettingsIcon, MessageCircleIcon } from "../components/icons";
import { usePublicPageScroll } from "../hooks/usePublicPageScroll";
import "../styles/info-pages.css";

export default function DocsPage() {
  const currentYear = new Date().getFullYear();

  usePublicPageScroll();

  useEffect(() => {
    document.title = "Documentation - Longhouse";
    const metaDescription = document.querySelector('meta[name="description"]');
    if (metaDescription) {
      metaDescription.setAttribute('content', 'Learn how to use Longhouse. Quick start guides, timeline search tips, and integration setup instructions.');
    }
  }, []);

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
          Get started with Longhouse.
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
            Requires Python 3.12+. No sudo needed.
          </p>

          <h3>2. Start the server</h3>
          <pre><code>longhouse serve</code></pre>
          <p>
            Opens a local web UI at <code>http://localhost:47300</code>.
            Data is stored in a SQLite database on your machine.
          </p>

          <h3>3. Use your AI coding tools</h3>
          <p>
            Keep using Claude Code, Codex CLI, or Gemini CLI as normal.
            Longhouse automatically discovers and imports your sessions
            from their default storage locations.
          </p>

          <h3>4. Browse and search</h3>
          <p>
            Open the timeline to see all your sessions. Use full-text search
            to find any conversation, tool call, or file edit across all your sessions.
          </p>
        </section>

        <section id="search" className="docs-section">
          <h2>Search</h2>
          <p>
            Longhouse provides full-text search across all your AI coding sessions.
            Search by keyword, file name, tool name, or any text from your conversations.
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
            Longhouse reads the session files that AI coding tools already produce.
            No plugins or configuration changes needed.
          </p>

          <h3>Fully Supported</h3>
          <ul>
            <li><strong>Claude Code</strong> — Reads from <code>~/.claude/projects/</code></li>
            <li><strong>Codex CLI</strong> — Reads from <code>~/.codex/</code></li>
            <li><strong>Gemini CLI</strong> — Reads from <code>~/.gemini/</code></li>
          </ul>

          <h3>Coming Soon</h3>
          <ul>
            <li><strong>Cursor</strong> — IDE-integrated AI sessions</li>
          </ul>

          <h3>How it works</h3>
          <p>
            A background shipper watches for new session files and imports them
            into the local SQLite database. Sessions are deduplicated by ID, so
            re-importing is safe and idempotent.
          </p>
        </section>

        <section id="config" className="docs-section">
          <h2>Configuration</h2>

          <h3>Authentication</h3>
          <p>
            By default, auth is disabled for local use. To add password protection:
          </p>
          <pre><code>LONGHOUSE_PASSWORD=your-password longhouse serve</code></pre>

          <h3>Port</h3>
          <p>
            Default port is 47300. Override with:
          </p>
          <pre><code>longhouse serve --port 8080</code></pre>

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
