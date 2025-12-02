import { useEffect } from "react";
import { Link } from "react-router-dom";
import { SwarmLogo } from "../components/SwarmLogo";
import "../styles/info-pages.css";

interface ChangelogEntry {
  version: string;
  date: string;
  title: string;
  changes: Array<{
    type: "new" | "improved" | "fixed";
    text: string;
  }>;
}

const changelog: ChangelogEntry[] = [
  {
    version: "0.4.0",
    date: "December 2024",
    title: "Landing Page Refresh",
    changes: [
      { type: "new", text: "Redesigned landing page with clearer value proposition" },
      { type: "new", text: "Added pricing, docs, privacy, and security pages" },
      { type: "improved", text: "Better mobile responsiveness across all pages" },
      { type: "improved", text: "Updated trust section with verifiable claims" },
    ],
  },
  {
    version: "0.3.0",
    date: "November 2024",
    title: "Visual Workflow Builder",
    changes: [
      { type: "new", text: "Canvas page with React Flow visual builder" },
      { type: "new", text: "Drag-and-drop workflow creation" },
      { type: "new", text: "Workflow save and load functionality" },
      { type: "improved", text: "Agent execution with real-time status updates" },
      { type: "fixed", text: "WebSocket reconnection handling" },
    ],
  },
  {
    version: "0.2.0",
    date: "October 2024",
    title: "Multi-Agent Chat",
    changes: [
      { type: "new", text: "Chat interface with streaming token responses" },
      { type: "new", text: "Thread management with rename and delete" },
      { type: "new", text: "Multiple agent support per workspace" },
      { type: "improved", text: "Dashboard with agent snapshots" },
      { type: "fixed", text: "Memory leaks in long-running sessions" },
    ],
  },
  {
    version: "0.1.0",
    date: "September 2024",
    title: "Initial Beta",
    changes: [
      { type: "new", text: "Core agent framework with LangGraph" },
      { type: "new", text: "Google OAuth authentication" },
      { type: "new", text: "Basic dashboard and profile management" },
      { type: "new", text: "Integration connector architecture" },
      { type: "new", text: "WebSocket real-time updates" },
    ],
  },
];

export default function ChangelogPage() {
  const currentYear = new Date().getFullYear();

  useEffect(() => {
    document.title = "Changelog - Swarmlet";
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
            <span className="info-page-brand-name">Swarmlet</span>
          </Link>
        </div>
      </header>

      <main className="info-page-content">
        <h1 className="info-page-title">Changelog</h1>
        <p className="info-page-subtitle">
          Track our progress. See what's new, improved, and fixed in each release.
        </p>

        <div className="changelog-list">
          {changelog.map((entry) => (
            <article key={entry.version} className="changelog-entry">
              <div className="changelog-version">
                <span className="changelog-version-tag">v{entry.version}</span>
                <span className="changelog-date">{entry.date}</span>
              </div>
              <h2 className="changelog-title">{entry.title}</h2>
              <ul className="changelog-changes">
                {entry.changes.map((change, idx) => (
                  <li key={idx}>
                    <span className={`changelog-badge ${change.type}`}>
                      {change.type}
                    </span>
                    {change.text}
                  </li>
                ))}
              </ul>
            </article>
          ))}
        </div>

        <div className="docs-section" style={{ marginTop: "var(--space-12)" }}>
          <h2>Stay Updated</h2>
          <p>
            Follow our development on <a href="https://github.com/swarmlet" target="_blank" rel="noopener noreferrer">GitHub</a> for
            the latest updates, or subscribe to our mailing list for major announcements.
          </p>
        </div>
      </main>

      <footer className="info-page-footer">
        <p>&copy; {currentYear} Swarmlet. All rights reserved.</p>
      </footer>
    </div>
  );
}
