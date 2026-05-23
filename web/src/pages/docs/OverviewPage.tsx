import { Link } from "react-router-dom";
import { usePageMeta } from "../../hooks/usePageMeta";

export default function OverviewPage() {
  usePageMeta({
    title: "Documentation - Longhouse",
    description: "Learn how to use Longhouse — mission control for your CLI agent sessions.",
  });

  return (
    <>
      <h1>Longhouse Documentation</h1>
      <p className="docs-subtitle">
        Mission control for CLI agent sessions running on machines you own.
        Import existing sessions fast, then start new work through Longhouse so
        it stays steerable for later.
      </p>

      <div className="docs-callout">
        <p>
          <strong>New here?</strong> Start with the{" "}
          <Link to="/docs/quickstart">Quick Start</Link> — you will have
          Longhouse running, your first session imported, and the normal launch
          path in under two minutes.
        </p>
      </div>

      <h2>What Longhouse Does</h2>
      <p>
        Longhouse puts Claude Code, Codex CLI, Antigravity CLI, and OpenCode sessions into one
        searchable timeline. Bare provider runs land as unmanaged history.
        When you launch a session through Longhouse, it becomes managed, so you
        can message, tail, or continue it later from the browser, CLI, or API.
      </p>
      <div className="docs-callout">
        <p>
          <strong>Managed vs unmanaged.</strong> Imported sessions are useful
          immediately for search and inspection, but they are not the long-term
          happy path. Only managed sessions keep a live control path.
        </p>
      </div>

      <h3>The core loop</h3>
      <ol>
        <li>
          <strong>Import</strong> — Longhouse reads the session files your CLI
          tools already produce. No workflow changes required.
        </li>
        <li>
          <strong>Find</strong> — Full-text search across every conversation,
          tool call, and file edit. Find the session where you already solved
          the problem.
        </li>
        <li>
          <strong>Inspect</strong> — Open the raw transcript and event history.
          The model already did the work; Longhouse makes it reusable.
        </li>
        <li>
          <strong>Control</strong> — When you launch through Longhouse, the
          session becomes managed and stays reachable. Send it a message, tail
          its events, or continue where it left off.
        </li>
      </ol>

      <h2>How It Runs</h2>
      <p>
        Longhouse is a single binary that runs a FastAPI server backed by SQLite.
        Your data stays on your machine. There are no external services to
        configure for the self-hosted path.
      </p>
      <p>
        It works on your laptop for quick lookups. It shines on a machine that
        stays on — a VPS, Mac mini, or homelab box — where sessions keep running
        after you close the lid.
      </p>

      <h2>Guides</h2>
      <nav className="docs-overview-grid">
        <Link to="/docs/quickstart" className="docs-overview-card">
          <h3>Quick Start</h3>
          <p>Install, open, find your first session.</p>
        </Link>
        <Link to="/docs/search" className="docs-overview-card">
          <h3>Search & Recall</h3>
          <p>Full-text search, filters, and recall across sessions.</p>
        </Link>
        <Link to="/docs/remote-control" className="docs-overview-card">
          <h3>Remote Control</h3>
          <p>Launch managed sessions and keep control after launch.</p>
        </Link>
        <Link to="/docs/cli" className="docs-overview-card">
          <h3>CLI Reference</h3>
          <p>Every command, flag, and output format.</p>
        </Link>
        <Link to="/docs/api" className="docs-overview-card">
          <h3>Machine API</h3>
          <p>The /api/agents/* surface for scripts and integrations.</p>
        </Link>
        <Link to="/docs/integrations" className="docs-overview-card">
          <h3>Integrations</h3>
          <p>Claude Code, Codex CLI, Antigravity CLI, OpenCode, and more.</p>
        </Link>
        <Link to="/docs/configuration" className="docs-overview-card">
          <h3>Configuration</h3>
          <p>Auth, ports, data location, and environment variables.</p>
        </Link>
      </nav>
    </>
  );
}
